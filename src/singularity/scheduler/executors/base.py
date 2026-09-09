"""executors.base — 统一执行器抽象

审计修了什么 (审计 6.4):
  - 三种调用方式 (claude-cli / claude-opus-cli / zhipu-api) 返回异构,
    validator 无法直接吃。定义 ExecutorResult 统一结构, 各 executor
    自己从原生输出提炼, validator 只吃这个结构, 不关心来源。
  - changed_files 由 executor 负责 (claude-cli 走 git diff, zhipu 走
    patch 文件), 不让 validator 反推。

v1 边界:
  - E+ (zhipu) 不自动落盘, 产出进 patch 文件, changed_files 为空直到 apply
    (审计 6.5)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExecutorResult:
    success: bool                       # executor 本身是否成功 (非 validate 结论)
    raw_output: str = ""                # 原始输出 (cli stdout / api content)
    changed_files: list = field(default_factory=list)  # 相对项目根的路径
    patch_path: Optional[str] = None    # E+ 智谱产出暂存路径 (未 apply)
    elapsed: float = 0.0
    token_count: int = 0                # token 消耗 (0=未获取)
    error: str = ""                     # 失败原因 (超时/限流/格式异常)
    error_kind: str = ""                # timeout | ratelimit | format | exec | ""
    tool_events: list = field(default_factory=list)   # 工具调用事件 [{tool,status,time,...}]


# ═══════════════════════════════════════════════════════════════
# Shared constants & error classes (ponytail: unified from zhipu + openai)
# ═══════════════════════════════════════════════════════════════

_BLOCKED_PATTERNS = [
    ".env", ".env.*", "*.token", "*.key", "*.pem", "*.p12", "*.pfx",
    "*.secret", "*.password", "*.credential",
    ".qidian/", ".qidian/*", ".git/", ".git/*", ".claude/",
    "venv/", ".venv/", "__pycache__/", "*.pyc",
    "users.json", "config.toml", "agents.toml",
]

_BLOCKED_COMMANDS = [
    "rm -rf /", "rm -rf ~", "rm -rf .",
    "curl", "wget",
    "chmod 777", "chmod -R",
    "sudo ", "su ",
    "mkfs.", "dd if=",
    ":(){ :|:& };:",
    "> /dev/sda",
    "shutdown", "reboot", "halt", "poweroff",
    "iptables", "nc -l", "nc -e",
    "python -c", "perl -e", "ruby -e", "bash -c",
    "eval ", "exec ",
]


def is_blocked_path(path: str) -> tuple[bool, str]:
    """敏感文件 blocklist 检查。返回 (blocked, reason)。

    统一入口: openai/zhipu/anthropic 三个 executor 共用 (原各自复制一份)。
    """
    import fnmatch
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


def is_dangerous_command(command: str) -> tuple[bool, str]:
    """危险命令检查。返回 (dangerous, reason)。"""
    cmd = command.strip()
    cmd_lower = cmd.lower()
    for blocked in _BLOCKED_COMMANDS:
        bl = blocked.lower()
        if cmd_lower.startswith(bl) or bl in cmd_lower:
            return True, f"危险命令被拦截: {blocked}"
    return False, ""


class ExecutorError(Exception):
    """Base executor error."""
    def __init__(self, msg: str, kind: str = "exec"):
        self.kind = kind
        super().__init__(msg)


class RateLimitError(ExecutorError):
    def __init__(self, msg: str = "rate limited"):
        super().__init__(msg, kind="ratelimit")


class FormatError(ExecutorError):
    def __init__(self, msg: str = "format error"):
        super().__init__(msg, kind="format")


class TimeoutError(ExecutorError):
    def __init__(self, msg: str = "timeout"):
        super().__init__(msg, kind="timeout")


class ExecError(ExecutorError):
    def __init__(self, msg: str = "execution error"):
        super().__init__(msg, kind="exec")


class BaseExecutor:
    """所有 executor 的基类。子类实现 run()。"""

    # 是否真能执行 cfg["no_tools"]（禁工具）。默认 False —— 没显式实现的执行器
    # 等于禁不掉（如 claude-cli 自带工具），调用方据此告警，而不是假装禁住了。
    honors_no_tools = False

    def __init__(self, agent_cfg: dict, task: str, task_id: str,
                 baseline_ref: str = "", cwd: str = "",
                 agent_level: str = "", **kwargs):
        self.cfg = agent_cfg
        self.task = task
        self.task_id = task_id
        self.baseline_ref = baseline_ref
        self.cwd = cwd
        self.agent_level = agent_level
        # ponytail: store injected kwargs for subclasses (skills, mcp_tools, etc.)
        for k, v in kwargs.items():
            setattr(self, f"_{k}", v)

    def run(self) -> ExecutorResult:
        raise NotImplementedError
