"""2026-09-14 那一批修复的守卫测试。

来源：外派 ZCode 的**四轮扫 bug + 三轮评审 + 一轮文档核对**，逐条翻代码核实后修的第一批。
每一组测试前面写明"它防的是什么"，免得以后有人把测试当噪声删掉。
详细取证见 `docs/扫bug-02-20260914.md` / `docs/扫bug-03-20260914.md`。
"""

from __future__ import annotations

import sys

import pytest

from singularity.scheduler._observer_answer import _is_gate_reply
from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor, _is_sensitive_env


# ── 人审门（GATE1/2/3）的回复判定 ────────────────────────────────
# 防的是：**"不通过"被当成"通过"**（子串倒挂），以及**聊天里顺口一句就把门放行**。

def test_不通过_走打回而不是批准():
    """头号 bug：批准分支排在前面，而 "不通过" 含子串 "通过" ⇒ 打回永远不生效。"""
    assert _is_gate_reply("不通过") == "rejected"
    assert _is_gate_reply("不通过，重做") == "rejected"
    assert _is_gate_reply("这方案不通过") == "rejected"


def test_聊天里顺口一句不会把门放过去():
    """"行"/"好"/"ok" 原来按子串算 ⇒ "这方案行不行？" 里的 "行" 直接放行。"""
    assert _is_gate_reply("这方案行不行") is None
    assert _is_gate_reply("之前的 bug 修好了吗") is None
    assert _is_gate_reply("你可以看一下这个") is None


def test_短词仍然认得出():
    """收紧不能把正常用法也收掉。"""
    assert _is_gate_reply("好") == "approved"
    assert _is_gate_reply("好的") == "approved"
    assert _is_gate_reply("可以") == "approved"
    assert _is_gate_reply("ok") == "approved"
    assert _is_gate_reply("行吧") == "approved"


def test_多字批准词出现在句子里也算():
    assert _is_gate_reply("确认，继续吧") == "approved"
    assert _is_gate_reply("我同意这个方案") == "approved"
    assert _is_gate_reply("通过") == "approved"


def test_空话不是门回复():
    assert _is_gate_reply("") is None
    assert _is_gate_reply("   ") is None


# ── CSRF 的本地来源判定 ─────────────────────────────────────────
# 防的是：IPv6 回环被判成非本地 ⇒ 绑 `::` 时整个 UI 的写操作 403。

def test_ipv6回环算本地():
    from singularity.web.app import _is_local_origin

    assert _is_local_origin("http://[::1]:5050") is True
    assert _is_local_origin("http://localhost:5050") is True
    assert _is_local_origin("http://127.0.0.1:5050") is True
    # 反例仍在：非本地不许过
    assert _is_local_origin("http://evil.com") is False
    assert _is_local_origin("http://127.0.0.1.evil.com") is False


# ── 子进程环境变量脱敏 ──────────────────────────────────────────
# 防的是：`MY_KEY` 这类名字漏给模型跑的命令（原来只做子串匹配，它不含 `API_KEY`）。

def test_env脱敏抓得住漏的那类():
    for name in ("MY_KEY", "SSH_KEY", "AWS_SECRET_KEY", "GITHUB_TOKEN",
                 "OPENAI_API_KEY", "DB_PASSWORD", "MY_CREDENTIAL"):
        assert _is_sensitive_env(name), f"{name} 应该被判敏感"


def test_env脱敏不误伤普通名字():
    """分段匹配而不是子串 —— 裸子串会把 MONKEY / KEYBOARD 也滤掉，那是过度过滤。"""
    for name in ("MONKEY", "KEYBOARD_LAYOUT", "HOCKEY", "PATH", "HOME"):
        assert not _is_sensitive_env(name), f"{name} 不该被判敏感"


# ── 敏感文件黑名单 ──────────────────────────────────────────────
# 防的是：`.env.local` / `id_rsa` 这类变体被读出全文。

def test_敏感文件名单兜住变体():
    from singularity.web.app import _SENSITIVE_FILES, _SENSITIVE_PREFIXES, _SENSITIVE_SUFFIXES

    def blocked(name: str) -> bool:
        # 复刻 app.py 里那条判据（三条 or）
        return (name in _SENSITIVE_FILES or name.startswith(_SENSITIVE_PREFIXES)
                or name.endswith(_SENSITIVE_SUFFIXES))

    for name in (".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
                 "server.key", "cert.pem", ".netrc"):
        assert blocked(name), f"{name} 应该被挡"
    # 反例：普通源码文件不许误伤
    for name in ("main.py", "README.md", "environment.py"):
        assert not blocked(name), f"{name} 不该被挡"


# ── 权限闸门的 fail-closed ──────────────────────────────────────
# 防的是：**检查器自己坏了 = 放行**（两条路都堵在这儿）。

class _StubExec:
    """只够跑 _check_permission 的最小替身。"""
    task_id = "t-1"
    _agent_level = "any"
    cfg = {"model": "m"}


def test_权限检查器正常时照常放行和拒绝():
    ok = _StubExec(); ok._permission_checker = lambda *a: (True, "")
    assert OpenAIAgentExecutor._check_permission(ok, "read_file", {}) == (True, "")

    no = _StubExec(); no._permission_checker = lambda *a: (False, "级别不够")
    assert OpenAIAgentExecutor._check_permission(no, "read_file", {}) == (False, "级别不够")


def test_权限检查器抛异常时拒绝():
    def _boom(*a):
        raise RuntimeError("checker 炸了")
    bad = _StubExec(); bad._permission_checker = _boom
    allowed, reason = OpenAIAgentExecutor._check_permission(bad, "run_command", {})
    assert allowed is False, "检查器异常必须 fail-closed"
    assert "拒绝" in reason


def test_没注入检查器时仍默认允许():
    """这条是**有意的默认**（没配权限就不拦），不是 fail-open —— 别一起改掉。"""
    bare = _StubExec(); bare._permission_checker = None
    assert OpenAIAgentExecutor._check_permission(bare, "read_file", {}) == (True, "")


def test_权限模块坏掉时不静默放行():
    """工厂自己失败原来 `return None` ⇒ 上游按"没注入"放行。现在必须拒绝 + 出声。"""
    from singularity.scheduler import _dispatch_skills as ds, witness

    warned: list[str] = []
    monkey_warn = lambda scope, msg: warned.append(msg)  # noqa: E731
    orig = witness.warn
    witness.warn = monkey_warn
    try:
        # 把 .permission 从 sys.modules 里变成"不可导入"，逼 `from .permission import …` 抛
        key = "singularity.scheduler.permission"
        saved = sys.modules.get(key, _MISSING := object())
        sys.modules[key] = None  # type: ignore[assignment]
        try:
            checker = ds._make_permission_checker()
        finally:
            if saved is _MISSING:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = saved  # type: ignore[assignment]
    finally:
        witness.warn = orig

    assert warned, "工厂失败必须出声"
    assert checker is not None, "不能返回 None（None = 上游放行）"
    assert checker("read_file", {}, "any", "m", "t-1")[0] is False, "必须按拒绝处理"


# ── 记忆链端点 ──────────────────────────────────────────────────
# 防的是：端点调一个全仓不存在的函数 ⇒ 每次必 500。

def test_记忆链端点真的能跑不再500():
    """**真调一次** —— 只断言 API 层有名是不够的：
    把 `_api_memory.memory_chain` 改回 `get_task_chain`，那种断言照样绿。

    原来端点调 `mem_mod.get_task_chain(...)`，而那个名字**全仓无定义**
    ⇒ AttributeError ⇒ 每次必 500。
    """
    from singularity.scheduler import _api_memory

    body, code = _api_memory.memory_chain("no-such-task-id-at-all")
    assert code == 404, f"查不到的记忆链应当 404，实际 {code} / {body}"
