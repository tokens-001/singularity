"""executors.zhipu_api — E+ 层智谱 HTTP API 执行器

审计修了什么 (审计 5.3 / 6.1 / 6.5):
  - 完整错误处理: 超时 / 限流(429指数退避) / 格式异常(不重试) 三类分开
  - request_template 从 yaml 渲染, entry 只是 URL (审计 6.1)
  - 产出不自动落盘: 写进 patch 文件, apply 前不碰磁盘 (审计 6.5)
    —— 保住 E+ 的安全边界, 和原人肉中转一致
  - Opus 审查: 补 URLError (DNS/连接拒绝) 捕获, 防穿透崩主程序

v1 边界:
  - 限流重试不计入 max_turns (max_turns 只计 validate 打回的重做)
  - 格式异常不重试 (可能是内容审查拦截, 重试无用)
  - changed_files 为空 (patch 未 apply), apply 是显式动作
"""

from __future__ import annotations
import fnmatch
import json
import os
import re
import ssl
import time
import urllib.request
import urllib.error

from .base import BaseExecutor, ExecutorResult
from .. import config

# ── 敏感文件 blocklist（防 LLM 输出注入，与 openai_agent.py 保持一致）──
_BLOCKED_PATTERNS = [
    ".env", ".env.*",
    "*.token", "*.key", "*.pem", "*.p12", "*.pfx",
    "*.secret", "*.password", "*.credential",
    ".qidian/", ".qidian/*",
    ".git/", ".git/*",
    ".claude/",
    "venv/", ".venv/",
    "__pycache__/",
    "*.pyc",
    "users.json",
    "config.toml", "agents.toml",
]


class ZhipuApiExecutor(BaseExecutor):
    """智谱 GLM-5.2 HTTP API 执行器。"""

    def run(self) -> ExecutorResult:
        api_key = os.environ.get(self.cfg.get("api_key_env", ""))
        if not api_key:
            return ExecutorResult(
                success=False,
                error=f"环境变量 {self.cfg.get('api_key_env')} 未设置",
                error_kind="exec",
            )

        url = self.cfg["entry"]
        body = self._render_body()
        start = time.time()

        # 限流重试 (审计 5.3): 指数退避, 不计入 max_turns
        for attempt in range(config.ZHIPU_MAX_RETRIES + 1):
            try:
                content, token_count = self._post(url, body, api_key)
                elapsed = time.time() - start
                return self._save_as_patch(content, elapsed, token_count)
            except _RateLimitError:
                if attempt < config.ZHIPU_MAX_RETRIES:
                    backoff = config.ZHIPU_BACKOFF_BASE * (2 ** attempt)
                    time.sleep(backoff)
                    continue
                return ExecutorResult(
                    success=False,
                    error=f"限流, 重试 {config.ZHIPU_MAX_RETRIES} 次仍失败",
                    error_kind="ratelimit",
                    elapsed=time.time() - start,
                )
            except _FormatError as e:
                # 格式异常不重试 (审计 5.3): 可能内容审查拦截
                return ExecutorResult(
                    success=False, error=f"响应格式异常: {e}",
                    error_kind="format", elapsed=time.time() - start,
                )
            except _ExecError as e:
                return ExecutorResult(
                    success=False, error=f"网络错误: {e}",
                    error_kind="exec", elapsed=time.time() - start,
                )
            except _TimeoutError:
                return ExecutorResult(
                    success=False,
                    error=f"超时 {config.ZHIPU_API_TIMEOUT}s",
                    error_kind="timeout",
                    elapsed=config.ZHIPU_API_TIMEOUT,
                )

    # ── 内部 ──────────────────────────────────────────────────────────
    def _render_body(self) -> dict:
        tmpl = self.cfg.get("request_template", {})
        # 深拷贝并渲染 {prompt}
        body = json.loads(json.dumps(tmpl))
        _render_recursive(body, {"prompt": self.task})
        return body

    def _post(self, url: str, body: dict, api_key: str) -> str:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=config.ZHIPU_API_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise _RateLimitError()
            raise _FormatError(f"HTTP {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            raise _ExecError(f"网络不可达: {e.reason}")
        except ssl.SSLError as e:
            raise _ExecError(f"SSL错误: {e}")
        except TimeoutError:
            raise _TimeoutError()

        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
            # 成本: 取 API 返回的 usage 信息
            usage = data.get("usage", {})
            token_count = (
                usage.get("total_tokens", 0)
                or usage.get("completion_tokens", 0) + usage.get("prompt_tokens", 0)
            )
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            raise _FormatError(f"{e}; raw[:200]={raw[:200]}")

        if not content or not content.strip():
            raise _FormatError("content 为空 (可能内容审查拦截)")
        return content, token_count

    def _save_as_patch(self, content: str, elapsed: float, token_count: int = 0) -> ExecutorResult:
        """产出写进 patch 文件。解析 @files 头提取改动文件列表。"""
        changed_files = []
        meta_match = re.match(r'\s*<!--\s*@files:\s*(.+?)\s*-->', content)
        if meta_match:
            changed_files = [f.strip() for f in meta_match.group(1).split(",") if f.strip()]

        patch_path = config.PATCH_DIR / f"{self.task_id}.md"
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text(content, encoding="utf-8")
        return ExecutorResult(
            success=True,
            raw_output=content,
            patch_path=str(patch_path),
            changed_files=changed_files,
            elapsed=elapsed,
            token_count=token_count,
        )

    @staticmethod
    def _is_blocked_path(path: str) -> tuple[bool, str]:
        """检查路径是否命中敏感文件 blocklist。返回 (blocked, reason)。"""
        normalized = path.replace("\\", "/")
        for pattern in _BLOCKED_PATTERNS:
            if fnmatch.fnmatch(normalized, pattern):
                return True, f"敏感文件/目录: {pattern}"
            if fnmatch.fnmatch(normalized, f"*/{pattern}"):
                return True, f"敏感文件/目录: {pattern}"
            parts = normalized.split("/")
            for part in parts:
                if fnmatch.fnmatch(part, pattern.rstrip("/*")):
                    return True, f"敏感文件/目录: {pattern}"
        return False, ""

    @staticmethod
    def apply_patch(task_id: str) -> dict:
        """读取 patch 文件，解析 @files 头，提取代码块写入目标文件。

        返回 {"applied": [...], "failed": [...]}。
        """
        patch_path = config.PATCH_DIR / f"{task_id}.md"
        if not patch_path.exists():
            return {"applied": [], "failed": [], "error": f"patch 不存在: {patch_path}"}

        content = patch_path.read_text(encoding="utf-8")
        meta_match = re.match(r'\s*<!--\s*@files:\s*(.+?)\s*-->', content)
        if not meta_match:
            return {"applied": [], "failed": [], "error": "patch 无 @files 声明"}

        declared_files = [f.strip() for f in meta_match.group(1).split(",") if f.strip()]
        if not declared_files:
            return {"applied": [], "failed": []}

        code_blocks = re.findall(r'```(?:\w*)\s*\n(.*?)```', content, re.DOTALL)
        applied, failed = [], []
        for i, target_file in enumerate(declared_files):
            if i < len(code_blocks):
                block = code_blocks[i]
            else:
                failed.append({"file": target_file, "error": "无对应代码块"})
                continue
            # 安全加固：路径穿越检查
            root = config.PROJECT_ROOT.resolve()
            dest = (config.PROJECT_ROOT / target_file).resolve()
            if not str(dest).startswith(str(root)):
                failed.append({"file": target_file, "error": "路径逃逸被拒绝"})
                continue
            # 安全加固：敏感文件 blocklist
            blocked, reason = ZhipuApiExecutor._is_blocked_path(target_file)
            if blocked:
                failed.append({"file": target_file, "error": reason})
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(block, encoding="utf-8")
                applied.append(target_file)
            except OSError as e:
                failed.append({"file": target_file, "error": str(e)})

        return {"applied": applied, "failed": failed}


# ── 渲染辅助 ──────────────────────────────────────────────────────────
def _render_recursive(obj, mapping: dict):
    if isinstance(obj, dict):
        for k in obj:
            obj[k] = _render_recursive(obj[k], mapping)
        return obj
    if isinstance(obj, list):
        return [_render_recursive(x, mapping) for x in obj]
    if isinstance(obj, str):
        for k, v in mapping.items():
            obj = obj.replace("{" + k + "}", v)
        return obj
    return obj


# ── 错误类型 ──────────────────────────────────────────────────────────
class _RateLimitError(Exception):
    pass


class _FormatError(Exception):
    pass


class _TimeoutError(Exception):
    pass


class _ExecError(Exception):
    """网络层错误 (DNS/连接拒绝), 不重试, 优雅降级。"""
    pass
