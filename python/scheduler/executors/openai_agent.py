"""executors.openai_agent — 通用Agent Runtime。

不依赖 Claude Code CLI。任意 OpenAI 兼容 API 都能用。
给模型装上手脚: 读文件、写代码、跑命令、搜代码。

协议: OpenAI function calling (tools API)。
支持: Kimi / GLM / DeepSeek API / Qwen / 任何 /v1/chat/completions。
"""

from __future__ import annotations
import json
import os
import re
import ssl
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from .base import BaseExecutor, ExecutorResult
from .. import config

# ── Tool 定义 (OpenAI function calling 格式) ──

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取一个文件的完整内容。改代码前必须先读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于项目根目录的文件路径"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入文件内容。会覆盖已有文件或创建新文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于项目根目录的文件路径"},
                    "content": {"type": "string", "description": "要写入的完整文件内容"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "运行终端命令。用于运行测试、lint、安装依赖等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的shell命令"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "在项目中搜索匹配的代码行。用于找到相关代码、理解调用关系。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "搜索模式（支持正则）"},
                    "path": {"type": "string", "description": "搜索的子目录，为空则全项目搜索"}
                },
                "required": ["pattern"]
            }
        }
    },
]

# 系统提示：告诉模型怎么用工具
SYSTEM_PROMPT = """你是奇点调度平台的 AI Agent。你有工具可以用：读文件、写代码、跑命令、搜代码。

工作方式:
1. 先读相关文件，理解上下文
2. 修改代码
3. 跑测试/编译验证
4. 如果测试失败，读错误信息、修复、重试
5. 完成后输出一句话总结

规则:
- 改代码前必须 read_file 看原文件
- 不要在注释里留 TODO，要么实现要么删掉
- 参数 path 是相对于项目根目录的路径，不要用绝对路径
- 写文件时给完整内容，不只给 diff"""


class OpenAIAgentExecutor(BaseExecutor):
    """通用 Agent Executor — 给任何 OpenAI 兼容模型装上工具。"""

    def __init__(self, cfg: dict, task: str, task_id: str,
                 baseline_ref: str = "", cwd: str = ""):
        super().__init__(cfg, task, task_id, baseline_ref=baseline_ref, cwd=cwd)
        self._api_key = os.environ.get(cfg.get("api_key_env", ""), "")
        self._url = cfg.get("entry", "")
        self._model = cfg.get("request_template", {}).get("model", cfg.get("model", ""))
        self._max_turns = cfg.get("max_turns", 10)
        self._cwd = Path(cwd) if cwd else config.PROJECT_ROOT
        self._changed_files: list[str] = []

    def run(self) -> ExecutorResult:
        if not self._api_key:
            return ExecutorResult(success=False, error=f"API key 未设置: {self.cfg.get('api_key_env','')}")

        start = time.time()
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self.task},
        ]
        total_tokens = 0

        for turn in range(1, self._max_turns + 1):
            body = {
                "model": self._model,
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": "auto",
                "temperature": 0.3,
                "max_tokens": 4096,
            }

            try:
                resp_data = self._api_call(body)
            except _RateLimitError:
                time.sleep(2 ** turn)
                continue
            except (_NetworkError, _FormatError) as e:
                return ExecutorResult(success=False, error=str(e),
                                      error_kind="exec", elapsed=time.time() - start)

            choice = resp_data.get("choices", [{}])[0]
            msg = choice.get("message", {})
            total_tokens += resp_data.get("usage", {}).get("total_tokens", 0)
            messages.append(msg)

            # 有 tool_calls → 执行工具
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    args = json.loads(func.get("arguments", "{}"))
                    result = self._execute_tool(name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result,
                    })
                continue  # 继续下一轮，让模型看工具结果

            # 无 tool_calls → 任务完成
            content = msg.get("content", "")
            if content.strip():
                elapsed = time.time() - start
                # 用 git diff 追踪改动的文件
                self._track_changed_files()
                return ExecutorResult(
                    success=True, raw_output=content,
                    changed_files=list(self._changed_files),
                    elapsed=elapsed, token_count=total_tokens,
                )

        return ExecutorResult(success=False,
                              error=f"达到最大轮次 {self._max_turns}，任务未完成",
                              error_kind="exec", elapsed=time.time() - start)

    # ── 工具执行 ──

    def _execute_tool(self, name: str, args: dict) -> str:
        try:
            if name == "read_file":
                return self._tool_read(args.get("path", ""))
            elif name == "write_file":
                return self._tool_write(args.get("path", ""), args.get("content", ""))
            elif name == "run_command":
                return self._tool_run(args.get("command", ""))
            elif name == "search_code":
                return self._tool_search(args.get("pattern", ""), args.get("path", ""))
            return f"未知工具: {name}"
        except Exception as e:
            return f"工具执行错误: {e}"

    def _safe_path(self, path: str) -> Path:
        """安全检查: 解析路径，禁止逃出项目目录。"""
        p = (self._cwd / path).resolve()
        root = self._cwd.resolve()
        if not str(p).startswith(str(root)):
            raise ValueError(f"路径逃逸被拒绝: {path} → {p}")
        return p

    def _tool_read(self, path: str) -> str:
        p = self._safe_path(path)
        if not p.exists():
            return f"文件不存在: {path}"
        content = p.read_text(encoding="utf-8")
        if len(content) > 8000:
            return content[:8000] + f"\n... (截断，共 {len(content)} 字符)"
        return content

    def _tool_write(self, path: str, content: str) -> str:
        p = self._safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self._changed_files.append(str(p.relative_to(self._cwd)))
        return f"已写入 {path} ({len(content)} 字符)"

    def _tool_run(self, command: str) -> str:
        # 安全: 禁止危险命令
        dangerous = ["rm -rf /", "sudo", "mkfs", "dd if=", ":(){ :|:& };:"]
        for d in dangerous:
            if d in command:
                return f"命令被拒绝: 含危险操作 '{d}'"
        try:
            r = subprocess.run(command, shell=True, capture_output=True,
                               text=True, timeout=30, cwd=str(self._cwd))
            out = r.stdout[-4000:] if r.stdout else ""
            err = r.stderr[-2000:] if r.stderr else ""
            return f"exit={r.returncode}\nstdout:\n{out}\nstderr:\n{err}"
        except subprocess.TimeoutExpired:
            return "命令超时 (30s)"

    def _tool_search(self, pattern: str, path: str = "") -> str:
        search_dir = self._safe_path(path) if path else self._cwd
        try:
            results = []
            for f in search_dir.rglob("*.py"):
                if ".qidian" in str(f) or "venv" in str(f) or "__pycache__" in str(f):
                    continue
                try:
                    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                        if re.search(pattern, line):
                            results.append(f"{f.relative_to(self._cwd)}:{i}: {line.strip()[:120]}")
                            if len(results) > 20:
                                return "\n".join(results) + "\n... (截断)"
                except Exception:
                    pass
            return "\n".join(results) if results else f"未找到匹配 '{pattern}' 的行"
        except Exception as e:
            return f"搜索错误: {e}"

    def _track_changed_files(self):
        """通过 git diff 追踪改动的文件。"""
        try:
            r = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True, text=True, cwd=str(config.PROJECT_ROOT),
            )
            if r.returncode == 0:
                for f in r.stdout.strip().splitlines():
                    if f and f not in self._changed_files:
                        self._changed_files.append(f)
        except Exception:
            pass

    # ── API 调用 ──

    def _api_call(self, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=data, method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise _RateLimitError()
            raise _FormatError(f"HTTP {e.code}")
        except urllib.error.URLError as e:
            raise _NetworkError(f"网络错误: {e.reason}")
        except ssl.SSLError as e:
            raise _NetworkError(f"SSL错误: {e}")
        except TimeoutError:
            raise _NetworkError("超时")

        try:
            data = json.loads(raw)
            if "error" in data:
                raise _FormatError(f"API错误: {data['error']}")
            return data
        except json.JSONDecodeError as e:
            raise _FormatError(f"JSON解析失败: {e}")


# ── 错误类型 ──

class _RateLimitError(Exception): pass
class _FormatError(Exception): pass
class _NetworkError(Exception): pass
