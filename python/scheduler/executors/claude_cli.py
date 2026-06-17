"""executors.claude_cli — E/D 层 CLI 执行器

审计修了什么:
  - entry 模板渲染 {prompt}, 不字符串拼接 (审计 Q2 注入风险)
  - changed_files 走 git diff, 不让 agent 自报 (审计 6.4)
  - 超时硬截断 (审计 5.3)

Opus 审查修了什么:
  - shell=True → shell=False: 别名不认 + 中文/JSON 被 shell 打碎 + 注入风险
  - entry 用 shlex 拆 argv 模板, prompt 作为独立 argv 元素传入
  - agent cfg 支持 env / env_unset 字段, D 层可切换 API endpoint

v1 边界:
  - 只支持 E (deepseek) / D (opus) 两层
  - 假设 agent 直接改磁盘文件 (claude -p 模式), snapshot 在调用前已做
"""

from __future__ import annotations
import os
import re
import shlex
import subprocess
import time

from .base import BaseExecutor, ExecutorResult
from .. import config

# 从 stdout/stderr 中尝试匹配 token 数 (多模型格式不同, 尽力解析)
_TOKEN_PATTERNS = [
    re.compile(r"total[_\s]?tokens?[:\s]+(\d[\d,]*)", re.IGNORECASE),
    re.compile(r"token[_\s]?usage[:\s]+(\d[\d,]*)", re.IGNORECASE),
    re.compile(r"tokens?[:\s]+(\d[\d,]+)", re.IGNORECASE),
    re.compile(r"(\d[\d,]+)\s+tokens?", re.IGNORECASE),
]

def _parse_token_count(stdout: str, stderr: str = "") -> int:
    """从 CLI 输出中尽力解析 token 消耗, 未匹配返回 0。"""
    combined = f"{stdout}\n{stderr}"
    for pat in _TOKEN_PATTERNS:
        m = pat.search(combined)
        if m:
            return int(m.group(1).replace(",", ""))
    return 0


class ClaudeCliExecutor(BaseExecutor):
    """claude / claude-opus 的 -p 模式执行器。"""

    def run(self) -> ExecutorResult:
        entry_tmpl = self.cfg.get("entry", "")
        if "{prompt}" not in entry_tmpl:
            return ExecutorResult(
                success=False, error="entry 缺 {prompt} 占位", error_kind="exec"
            )

        # shlex 拆模板 → 逐 arg 替换 {prompt} → prompt 永远是独立 argv 元素
        # (不会进 shell, 中文/JSON/特殊字符 都不被解析)
        tmpl_args = shlex.split(entry_tmpl)
        argv = []
        for a in tmpl_args:
            argv.append(a.replace("{prompt}", self.task))

        # 构建子进程环境: 继承当前 env + agent 配置覆盖
        env = os.environ.copy()
        for k, v in self.cfg.get("env", {}).items():
            val = str(v)
            # {VAR} → 从当前环境取值 (如 {ANTHROPIC_API_KEY_OPS})
            if val.startswith("{") and val.endswith("}"):
                ref = val[1:-1]
                val = os.environ.get(ref, "")
            env[k] = val
        for k in self.cfg.get("env_unset", []):
            env.pop(k, None)

        start = time.time()
        # cwd: worktree 沙箱优先, 否则项目根
        run_cwd = self.cwd if self.cwd else str(config.PROJECT_ROOT)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True, text=True,
                timeout=config.CLAUDE_CLI_TIMEOUT,
                cwd=run_cwd,
                env=env,
            )
        except FileNotFoundError:
            return ExecutorResult(
                success=False,
                error=f"二进制不存在: {argv[0]}",
                error_kind="exec",
            )
        except subprocess.TimeoutExpired:
            return ExecutorResult(
                success=False,
                error=f"超时 {config.CLAUDE_CLI_TIMEOUT}s",
                error_kind="timeout",
                elapsed=config.CLAUDE_CLI_TIMEOUT,
            )

        elapsed = time.time() - start
        if proc.returncode != 0:
            return ExecutorResult(
                success=False,
                error=f"exit={proc.returncode}: {proc.stderr[:200]}",
                error_kind="exec",
                elapsed=elapsed,
                token_count=_parse_token_count(proc.stdout, proc.stderr),
            )

        # changed_files: diff 对 snapshot 基线, 不把脏改动算 agent 头上
        changed = _git_changed_files(self.baseline_ref, run_cwd)

        return ExecutorResult(
            success=True,
            raw_output=proc.stdout,
            changed_files=changed,
            elapsed=elapsed,
            token_count=_parse_token_count(proc.stdout, proc.stderr),
        )


def _git_changed_files(baseline_ref: str = "", cwd: str = "") -> list:
    """本次任务改动的文件 (对 snapshot 基线做 diff, 不把脏改动算 agent 头上)。

    - tracked 变更: git diff <baseline_ref> --name-only
    - untracked 新建: git ls-files --others --exclude-standard
      注意: untracked 无法区分 agent 建的还是预先就有的 (git 不跟踪),
      v1 全量纳入, 宁可多报不漏报。
    cwd 默认项目根; worktree 沙箱时传 wt.path。
    """
    run_cwd = cwd if cwd else str(config.PROJECT_ROOT)
    changed = []
    try:
        diff_args = ["git", "diff", "--name-only"]
        if baseline_ref:
            diff_args.append(baseline_ref)
        proc = subprocess.run(
            diff_args,
            capture_output=True, text=True,
            cwd=run_cwd,
        )
        if proc.returncode == 0:
            changed.extend(f for f in proc.stdout.strip().splitlines() if f)

        # untracked 新建文件 (checkout 删不掉的那些)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True,
            cwd=run_cwd,
        )
        if untracked.returncode == 0:
            changed.extend(f for f in untracked.stdout.strip().splitlines() if f)
    except Exception:  # noqa: BLE001
        pass

    # 排除调度器基础设施 (.qidian/ 是 snapshot/trace/patch 产物, 不是 agent 改动)
    return [f for f in changed if not f.startswith(".qidian")]
