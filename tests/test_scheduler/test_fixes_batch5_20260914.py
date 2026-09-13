"""第五批修复（2026-09-14 收尾段）—— 每条都钉在"删掉那一行它会红"上。

出处：外派 ⑨/⑩（`复核驳回-01` / `按形状扫-01`，我逐条核过调用链）+ `OPEN.md` 接手指针。
每条都在**当前树**上重读过，不是照抄报告。
"""
import subprocess
import types

import pytest

# 必须先导 dispatcher：它末尾 `from _dispatch_exec import *`，而 `_dispatch_exec`
# 反过来在模块级 `from dispatcher import (...)` —— 单独导 `_dispatch_exec` 会炸
# （全仓已知、故意不修的"一个毂 + 三根辐条"，见 OPEN.md）。测试里先导枢纽绕开。
from singularity.scheduler import dispatcher  # noqa: F401


# ═══════════════════════════════════════════════════════════════
# ① validator.run_project_tests —— unittest 报 0 个测试时不能算"通过"
# ═══════════════════════════════════════════════════════════════
# Python **<3.12** 的 `python -m unittest discover` 在没找到测试时打印的是
# `Ran 0 tests in 0.000s` + `OK`，**退出码 0**（3.12 起才改成 "NO TESTS RAN" + rc=5）。
# 原来的匹配串只认 "no tests ran"，对不上这句话 ⇒ 一路落到 `rc == 0` 那支
# ⇒ 把"一个测试都没有"报成**"通过、0 个用例"**。
# ⚠️ 而 `pyproject.toml` 声明的地板就是 3.11 ⇒ 这不是历史包袱，是活的口子。

def _fake_run(stdout: str, code: int):
    def _run(cmd, **kw):
        return types.SimpleNamespace(stdout=stdout, stderr="", returncode=code)
    return _run


def test_老版本_unittest_的_0_测试不算通过(monkeypatch, tmp_path):
    """**变异判据**：去掉 `"ran 0 tests" in low` 这一句，本用例必须红
    （会走到 `rc == 0` 那支，runner 变成 "unittest"、passed 保持 True）。"""
    real_run = subprocess.run

    def _run(cmd, **kw):
        if cmd[1:3] == ["-m", "unittest"]:
            # 3.9/3.11 的原话，逐字抄的（rc=0 是关键）
            return types.SimpleNamespace(
                stdout="Ran 0 tests in 0.000s\n\nOK\n", stderr="", returncode=0)
        raise FileNotFoundError(cmd[0])   # pytest / npm 都不存在
    monkeypatch.setattr(subprocess, "run", _run)

    from singularity.scheduler.validator import run_project_tests
    r = run_project_tests(cwd=str(tmp_path))
    assert r["runner"] == "none", (
        f"runner={r['runner']!r} —— 报成了「跑过了」，而它其实一个测试都没找到")
    assert "没找到测试" in r["output"], r["output"]
    assert r["passed"] is True, "没找到测试 ≠ 测试挂了（不能矫枉过正）"


def test_有测试时不受这句话影响(monkeypatch, tmp_path):
    """反向保护：`Ran 1 tests` 这类正常输出不能被新判据误伤成"没找到测试"。"""
    def _run(cmd, **kw):
        if cmd[1:3] == ["-m", "unittest"]:
            return types.SimpleNamespace(
                stdout="Ran 3 tests in 0.001s\n\nOK\n", stderr="", returncode=0)
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(subprocess, "run", _run)

    from singularity.scheduler.validator import run_project_tests
    r = run_project_tests(cwd=str(tmp_path))
    assert r["runner"] == "unittest", f"正常跑通的 unittest 被误判：{r}"
    assert r["passed"] is True


# ═══════════════════════════════════════════════════════════════
# ② 权限工具门：只在 openai 执行器上生效 ⇒ 收进 BaseExecutor
# ═══════════════════════════════════════════════════════════════
# `grep -c permission` 五个执行器文件：`base`/`anthropic_api`/`claude_cli`/`zhipu_api`
# **全是 0**，只有 `openai_agent` 是 8。⇒ 同一个 agent 换个 `type`，
# `allowed_tools` 白名单 + `require_approval` 审批通道 + profile 黑名单**整层静默消失**，
# 而界面上照样显示"已绑定"。**两个外派窗口用两套方法独立撞上同一处。**

def _deny_writes(tool, args, level, model, task_id):
    if tool in ("write_file", "run_command"):
        return False, f"profile=read-only 不许 {tool}"
    return True, ""


def test_anthropic_执行器也要过权限闸门(tmp_path):
    """**变异判据**：删掉 `_execute_tool` 顶部那句 `self._check_permission(...)`，
    本用例必须红 —— 文件会真的落盘、返回值变成"已写入"。"""
    from singularity.scheduler.executors.anthropic_api import AnthropicApiExecutor

    calls = []

    def checker(tool, args, level, model, task_id):
        calls.append((tool, level, model))
        return _deny_writes(tool, args, level, model, task_id)

    ex = AnthropicApiExecutor(agent_cfg={"model": "claude-sonnet-4-6"}, task="t",
                              task_id="tid", cwd=str(tmp_path), agent_level="ops",
                              permission_checker=checker)
    out = ex._execute_tool("write_file", {"path": "a.txt", "content": "hi"})
    assert "操作被拒绝" in out, f"闸门没拦：{out!r}"
    assert not (tmp_path / "a.txt").exists(), "闸门说拒绝，文件却落盘了"
    assert calls and calls[0][0] == "write_file", "检查器根本没被调用（闸门没接线）"
    # 读文件不该被误伤
    assert "操作被拒绝" not in ex._execute_tool("search_code", {"pattern": "x"})


def test_权限检查器抛异常时按拒绝处理(tmp_path):
    """fail-closed：检查器一死，闸门不能跟着消失。"""
    from singularity.scheduler.executors.anthropic_api import AnthropicApiExecutor

    def boom(*a, **k):
        raise RuntimeError("checker 内部炸了")

    ex = AnthropicApiExecutor(agent_cfg={}, task="t", task_id="tid",
                              cwd=str(tmp_path), agent_level="ops",
                              permission_checker=boom)
    out = ex._execute_tool("write_file", {"path": "a.txt", "content": "hi"})
    assert "操作被拒绝" in out and "RuntimeError" in out, f"异常没被当成拒绝：{out!r}"
    assert not (tmp_path / "a.txt").exists()


def test_openai_执行器把_agent_level_传给了闸门(tmp_path):
    """**接线钉子**：闸门收进基类后读的是 `self.agent_level`。

    而这边的 `__init__` 原来**不往父类传** `agent_level`（它自己存 `self._agent_level`）
    ⇒ 基类那份拿到 "" ⇒ profile 按 `""/model` 查表、**永远查成 full-access**
    —— 闸门看着在，绑的 profile 一条也用不上。
    变异：把那句 `agent_level=agent_level or cfg.get("_level","")` 去掉 → 本用例红。
    """
    from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor

    seen = []

    def checker(tool, args, level, model, task_id):
        seen.append((level, model))
        return True, ""

    ex = OpenAIAgentExecutor({"model": "m", "api_key_env": "NOPE_KEY"}, "t", "tid",
                             cwd=str(tmp_path), agent_level="ops",
                             permission_checker=checker)
    assert ex.agent_level == "ops", f"父类拿到的是 {ex.agent_level!r}"
    ex._check_permission("read_file", {})
    assert seen == [("ops", "m")], f"闸门收到的层/型号不对：{seen}"


# ── 没有本地工具面的执行器：拦不了，但**不许假装拦住了** ──

class _FakeProfile:
    def __init__(self, name):
        self.name = name


def _patch_profile(monkeypatch, profile_name):
    from singularity.scheduler import permission
    monkeypatch.setattr(permission, "get_store", lambda: type(
        "S", (), {"get_agent_profile": lambda self, l, m: _FakeProfile(profile_name)})())


@pytest.mark.parametrize("cls_name", ["ClaudeCliExecutor", "ZhipuApiExecutor"])
def test_拦不住的执行器绑了限制profile要出声(monkeypatch, cls_name):
    """`claude-cli` 的工具在它自己的子进程里、`zhipu` 只产 patch —— 本进程没有可拦的地方。

    两个后果都要挡住：① 不出声 = 用户以为 profile 生效了；
    ② 反过来给 `full-access` 也报一条 = 把真信号淹掉（下一条用例钉住这半边）。
    变异：删掉 `_warn_if_profile_not_enforceable` 的调用，本用例红。
    """
    from singularity.scheduler import _dispatch_exec as D, witness
    got = []
    monkeypatch.setattr(witness, "warn", lambda scope, msg, key="": got.append(msg))
    _patch_profile(monkeypatch, "read-only")
    cls = getattr(__import__(
        f"singularity.scheduler.executors.{'claude_cli' if 'Cli' in cls_name else 'zhipu_api'}",
        fromlist=["x"]), cls_name)
    D._warn_if_profile_not_enforceable(cls, "ops", "deepseek-flash")
    assert len(got) == 1, f"该报一条、实际 {got}"
    assert "profile_not_enforceable" in got[0] and "read-only" in got[0], got[0]


def test_full_access不算绑了profile(monkeypatch):
    """默认 profile 是 full-access（= 没绑），为它出声只会把真信号淹掉。"""
    from singularity.scheduler import _dispatch_exec as D, witness
    from singularity.scheduler.executors.claude_cli import ClaudeCliExecutor
    got = []
    monkeypatch.setattr(witness, "warn", lambda scope, msg, key="": got.append(msg))
    _patch_profile(monkeypatch, "full-access")
    D._warn_if_profile_not_enforceable(ClaudeCliExecutor, "ops", "m")
    assert got == [], f"full-access 不该出声：{got}"


def test_有工具面的执行器不走这条告警(monkeypatch):
    """有工具面的执行器是**真会拦**的，报"拦不住"就是假警报。
    变异：去掉那个 `has_tool_surface` 早退 → 本用例红。"""
    from singularity.scheduler import _dispatch_exec as D, witness
    from singularity.scheduler.executors.anthropic_api import AnthropicApiExecutor
    got = []
    monkeypatch.setattr(witness, "warn", lambda scope, msg, key="": got.append(msg))
    _patch_profile(monkeypatch, "read-only")
    D._warn_if_profile_not_enforceable(AnthropicApiExecutor, "ops", "m")
    assert got == [], f"有工具面却被报成拦不住：{got}"


# ═══════════════════════════════════════════════════════════════
# ③ 敏感路径/危险命令：一张表，不是三张
# ═══════════════════════════════════════════════════════════════
# 同一件事原来写了两遍：`executors/base` 的**硬地板**（所有 agent 都过）和
# `permission.SANDBOXED` 的**拦截承诺**（界面照着它显示）。两份**双向**不一致：
# 地板有 `id_rsa`/`*.pem` 而 sandboxed 没有；sandboxed 有 `rm -rf` 而地板只拦 `rm -rf /`。

def test_sandboxed_profile_不比硬地板松():
    """profile 是"更严的那一档"，**不能比地板松** —— 松了就是界面承诺 ≠ 实拦。

    变异：把 `blocked_paths` 换回手抄的那张短名单 → 红。
    （`rm -rf` 那条是**有意的额外**：地板拦 `rm -rf /`/`~`/`.`，沙箱连 `rm -rf build/` 也拦。）
    """
    from singularity.scheduler import _sensitive as S, permission as P
    assert set(S.BLOCKED_PATH_PATTERNS) <= set(P.SANDBOXED.blocked_paths), \
        "地板拦的路径，sandboxed 的承诺里缺：" \
        f"{sorted(set(S.BLOCKED_PATH_PATTERNS) - set(P.SANDBOXED.blocked_paths))}"
    assert set(S.BLOCKED_COMMANDS) <= set(P.SANDBOXED.blocked_commands), \
        f"{sorted(set(S.BLOCKED_COMMANDS) - set(P.SANDBOXED.blocked_commands))}"
    # 两处**曾经不一致**的，各钉一条（双向都要钉，不然只防住一半）
    assert "id_rsa" in P.SANDBOXED.blocked_paths, "地板有、承诺里没有过的那个"
    assert "rm -rf" in P.SANDBOXED.blocked_commands, "承诺有、地板没有过的那个"


def test_执行器的地板和_sensitive_是同一份():
    """**同一个对象**，不是"内容碰巧一样" —— 内容一样的两份明天就会漂。
    变异：把 base 里那张表改回手写列表 → 红。"""
    from singularity.scheduler import _sensitive as S
    from singularity.scheduler.executors import base as B
    assert B._BLOCKED_PATTERNS is S.BLOCKED_PATH_PATTERNS
    assert B._BLOCKED_COMMANDS is S.BLOCKED_COMMANDS
    # 走一遍真入口，确认搬完家行为没变（`.ssh/id_rsa` 逐段比那一路）
    assert B.is_blocked_path("/home/u/.ssh/id_rsa")[0] is True
    assert B.is_blocked_path("src/main.py")[0] is False
    assert B.is_dangerous_command("rm -rf /")[0] is True
    assert B.is_dangerous_command("pytest -q")[0] is False


def test_run_executor_真的调了这条告警(monkeypatch):
    """**接线**：上面几条测的是函数本体，"接线通不通"是另一回事
    —— 挪走/删掉 `_run_executor` 里那句调用，它们照样全绿。
    变异：删掉 `_run_executor` 里那句 `_warn_if_profile_not_enforceable(...)` → 本用例红。
    """
    from singularity.scheduler import _dispatch_exec as D
    from singularity.scheduler.executors.base import ExecutorResult

    called = []
    monkeypatch.setattr(D, "_warn_if_profile_not_enforceable",
                        lambda cls, lv, md: called.append((cls.__name__, lv, md)))
    monkeypatch.setattr(D, "_load_skills_for_agent", lambda *a, **k: ([], "", {}))
    monkeypatch.setattr(D, "_load_mcp_for_agent", lambda: ([], None))

    class _FakeExec:
        def __init__(self, *a, **k):
            pass

        def run(self):
            return ExecutorResult(success=True)

    D._run_executor(_FakeExec, {"model": "m"}, "任务", "tid", "ops")
    assert called == [("_FakeExec", "ops", "m")], f"没调（或调错了）：{called}"
