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

# ⚠️ **敏感路径 / 危险命令这两张表搬到 `scheduler/_sensitive.py` 了**（2026-09-14）。
# 它们原来**还抄了一份**在 `permission.SANDBOXED` 里当"profile 的拦截承诺"，
# 两份已经不一致（`id_rsa` 只有这份有、`rm -rf` 只有那份有），而界面显示的是那份
# ⇒ 承诺和实拦对不上。现在**一份表、两处 import**（见那个模块的 docstring）。
# 名字保持不变：全仓有别的模块 `from ...executors.base import _BLOCKED_PATTERNS`。
#
# 🔴 **下面这四行是再导出（re-export），不是死 import —— 别删**（2026-09-19）。
# 本模块自己确实一个都不用它们；用它们的是**别的模块**：
#   `_BLOCKED_PATTERNS`   ← `anthropic_api.py` + 2 个测试
#   `_BLOCKED_COMMANDS`   ← 2 个测试
#   `is_blocked_path`     ← `openai_agent.py` / `zhipu_api.py`
#   `is_dangerous_command`← `openai_agent.py`
#
# ⚠️ 下面四行的标记**不是懒惰，是必须的** —— 它们是再导出，
# 而 ruff **看不见跨模块的用途**。2026-09-19 一天踩了两次同一个坑：
#   · 完全没标记：`ruff check --fix` 把四行全删 ⇒ **53 个测试文件 ImportError**
#   · 只用 `as <同名>`（ruff 文档里认的"再导出"写法）：**改过名的那两个还是被删**
#     —— `is_blocked_path as is_blocked_path` 留住了，而
#     `BLOCKED_PATH_PATTERNS as _BLOCKED_PATTERNS` 被删。
#     ⇒ **ruff 的再导出识别要求别名和原名相同**，改名的一律当普通 import。
# ⇒ 改名的那两行只能挂一条**抑制注释**（见下面那两行的行尾标记）。
#   ⚠️ 这段说明本身**故意不写出那条指令的字面串** —— 写了的话 ruff 会把
#   **注释**也当成指令去解析，然后报一条 "Invalid noqa directive" 的假警告。
#
# 最后一道防线是本仓自己的守卫 `test_no_undefined_names.py::test_from_import_targets_exist`
# （扫全仓 `from M import N`，判 N 在不在 M 里）—— 但它**只扫 `src/`**，
# 测试目录里的引用它看不见，所以它只是兜底、不是保证。
# **想删这几行：先 `grep -rn "from .*executors.base import"`，再删。**
from singularity.scheduler._sensitive import (  # noqa: F401
    BLOCKED_COMMANDS as _BLOCKED_COMMANDS,
)
from singularity.scheduler._sensitive import (  # noqa: F401
    BLOCKED_PATH_PATTERNS as _BLOCKED_PATTERNS,
)
from singularity.scheduler._sensitive import (
    is_blocked_path as is_blocked_path,
)
from singularity.scheduler._sensitive import (
    is_dangerous_command as is_dangerous_command,
)


@dataclass
class ExecutorResult:
    success: bool                       # executor 本身是否成功 (非 validate 结论)
    raw_output: str = ""                # 原始输出 (cli stdout / api content)
    changed_files: list = field(default_factory=list)  # 相对项目根的路径
    patch_path: str | None = None    # E+ 智谱产出暂存路径 (未 apply)
    elapsed: float = 0.0
    token_count: int = 0                # token 消耗 (0=未获取)
    error: str = ""                     # 失败原因 (超时/限流/格式异常)
    error_kind: str = ""                # timeout | ratelimit | format | exec | ""
    tool_events: list = field(default_factory=list)   # 工具调用事件 [{tool,status,time,...}]


# ═══════════════════════════════════════════════════════════════
# Shared constants & error classes (ponytail: unified from zhipu + openai)
# ═══════════════════════════════════════════════════════════════

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
    _permission_checker: callable | None = None

    # 这次执行**还能花多少秒**（调用方按任务级死线倒推后传进来，见 `_run_executor`）。
    # 由 `_dispatch_exec._run_executor` 构造后赋值，所以放在**类属性**上而不是
    # `__init__` 参数里 —— 各执行器的签名不一（有的不吃 `**kwargs`），改构造签名
    # 会波及测试里的直接构造。`None` = 调用方不管 ⇒ 子类退回各自的老行为。
    budget_s: float | None = None

    def __init__(self, agent_cfg: dict, task: str, task_id: str,
                 baseline_ref: str = "", cwd: str = "",
                 agent_level: str = "", **kwargs):
        self.cfg = agent_cfg
        self.task = task
        self.task_id = task_id
        self.baseline_ref = baseline_ref
        self.cwd = cwd
        self.agent_level = agent_level
        # agent 配置里的 `env`（PATH / 代理 / endpoint 这类）—— 原来只有 openai 执行器
        # 在 `__init__` 里存它，于是 **anthropic 路上的工具子进程完全看不到它**
        # （外派 ⑩ 抓到、我核过）。提到基类：谁要用谁拿，`_run_command` 负责合并+脱敏。
        self._agent_env = dict(agent_cfg.get("env", {}) or {})
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
