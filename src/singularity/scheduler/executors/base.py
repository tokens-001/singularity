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
    # ⚠️ **无后缀的私钥**（2026-09-14，外派 K 条6 / H 反7 / E'⑤ 三方独立撞上）：
    # `*.key` / `*.pem` 罩不住 `id_rsa` 这一族 —— 模型一条 read_file 就能把私钥
    # 整读进上下文随输出带走。`web/app.py:1358` 的 `_SENSITIVE_FILES` 早就补了
    # 这几个、注释还点名了这个洞，**这张表当时没跟着改** —— 同一件事只修了一半。
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".netrc", ".flaskenv",
    # ⚠️ **`.crt` 不拦**（2026-09-14，逆向审抓到、我认）：证书是**公开材料**，
    # 拦它没有防泄露的价值，却会把"加 HTTPS / 配 mTLS"这类任务的显式读写**弄瘸**
    # （任务读不到自己的证书就没法干活）。带私钥的那几种后缀
    # （`*.key` / `*.pem` / `*.p12` / `*.pfx`）仍然拦着，覆盖了常规命名。
    ".ssh/", ".ssh/*",
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


_NO_CHECKER_WARNED: set[str] = set()


def _warn_no_checker_once(cls_name: str) -> None:
    """"这次运行没有权限闸门"——**每个执行器类只报一次**（不是每次工具调用）。

    报一次不是嫌吵：`witness.warn` 写的是 `config.QIDIAN_DIR/alerts.jsonl`，
    即**生产告警通道**，而直接构造执行器的还有测试和 `run_benchmark`。
    按调用报会把这条通道冲成噪音，而噪音的代价是**真事故被淹掉**。
    """
    if cls_name in _NO_CHECKER_WARNED:
        return
    _NO_CHECKER_WARNED.add(cls_name)
    # 局部 import 是为了让 base.py 保持"叶子模块"（它被各执行器导，别在这儿拉进整张图）。
    # 不用 try 包：`witness.warn` 自己的契约就是"记告警失败不再抛"（见它的 except 注释），
    # 多包一层只会变成**新的静默 except**（静默 except 棘轮当场就报了这两处）。
    from .. import witness as _w
    _w.warn("permission", f"no_permission_checker:{cls_name}"[:160])


class BaseExecutor:
    """所有 executor 的基类。子类实现 run()。"""

    # 是否真能执行 cfg["no_tools"]（禁工具）。默认 False —— 没显式实现的执行器
    # 等于禁不掉（如 claude-cli 自带工具），调用方据此告警，而不是假装禁住了。
    honors_no_tools = False

    # **本执行器有没有"本地工具面"** —— 即模型的每一次工具调用是不是都经本进程分发。
    # True 的执行器**必须**在分发处调 `self._check_permission()`；
    # False 的（claude-cli 自带工具、zhipu 只产 patch）**本进程没有可拦的地方** ——
    # 调用方必须**出声**，而不是让界面上的 profile 看起来生效了（同 `honors_no_tools` 的规矩）。
    # ⚠️ 2026-09-14：这之前只有 openai_agent 调权限检查，另外三个执行器
    # **一个 permission 引用都没有** ⇒ 绑了 read-only / sandboxed 的 agent
    # 换个执行器类型就能写盘、跑命令，白名单 + 审批通道 + profile 拦截**整层静默消失**。
    has_tool_surface = False

    # 权限检查回调。由 `_dispatch_exec._run_executor` 注入（见那里的注释）；
    # 类属性而不是构造参数，理由同 `budget_s`：各执行器签名不一。
    # `None` = 没人注入 ⇒ `_check_permission` 放行，但会**出声**（见下）。
    _permission_checker: "callable | None" = None

    # 这次执行**还能花多少秒**（调用方按任务级死线倒推后传进来，见 `_run_executor`）。
    # 由 `_dispatch_exec._run_executor` 构造后赋值，所以放在**类属性**上而不是
    # `__init__` 参数里 —— 各执行器的签名不一（有的不吃 `**kwargs`），改构造签名
    # 会波及测试里的直接构造。`None` = 调用方不管 ⇒ 子类退回各自的老行为。
    budget_s: "float | None" = None

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

    def _check_permission(self, tool_name: str, args: dict) -> tuple[bool, str]:
        """工具级权限闸门。**所有有本地工具面的执行器共用这一份**。

        原来它只长在 `openai_agent` 上（`grep -c permission` 其它三个执行器全是 0），
        于是同一个 agent 换个 `type` 就能绕开白名单 / 审批通道 / profile 黑名单。
        现在收进基类，谁有工具面谁在分发处调一次。

        两条 fail-closed（都是 2026-09-14 修的，之前是反的）：
          · 检查器**抛异常** → 拒绝（原来 `except: pass` → 落到下面的 `return True`）；
          · 检查器是 `_make_permission_checker` 失败时的产物 → 它返回的是 `_deny_all`，
            不是 `None`（那条也一起改过）。

        ⚠️ **`None` 仍然是放行**，这条**没改成拒绝**，因为改了会误伤：
        直接构造执行器的地方（测试、`_benchmark.run_benchmark`）本来就不注入 checker，
        而它们的 agent 没有绑 profile（`get_agent_profile` 默认返回 full-access）
        ⇒ 把 `None` 改成拒绝，是把"没绑 profile"错当成"绑了限制 profile"。
        **但降级必须出声**：没人注入 = 这次运行**没有权限那一道闸门**，盘上要留痕。
        （`_run_executor` 一定会注入，`_make_permission_checker` 失败时注入的是
        `_deny_all` 而不是 `None` ⇒ 正常调度**走不到**这一支，这只防"以后有人漏注入"。）
        """
        checker = getattr(self, "_permission_checker", None)
        if checker is None:
            _warn_no_checker_once(type(self).__name__)
            return True, ""
        try:
            return checker(tool_name, args, self.agent_level,
                           self.cfg.get("model", ""), self.task_id)
        except Exception as e:
            from .. import witness as _w
            _w.warn("permission",
                    f"perm_checker_error:{tool_name}:{type(e).__name__}"[:160])
            return False, f"权限检查器异常（{type(e).__name__}），按拒绝处理"
