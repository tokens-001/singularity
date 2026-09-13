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


# ═══════════════════════════════════════════════════════════════
# ④ 任务时间线：不许给"还没跑完"的任务画终点
# ═══════════════════════════════════════════════════════════════
# `tracker._TERMINAL = {done, failed, rolled_back}`，而 `task_timeline` 自己抄了一份
# 集合、把 `decomposed` / `conflict_held` 也当终点 —— 那两个**都要回调度循环**
# （decomposed 等子任务聚合、conflict_held 等人解决 merge 冲突）。
# 后果：排障时"任务卡在哪"会被画成一条"跑完了"的完整历程。

def _write_task(tid, **fields):
    import json
    from singularity.scheduler import tracker
    fields.setdefault("created_at", 1000)
    fields.setdefault("updated_at", 2000)
    (tracker.tasks_dir() / f"{tid}.json").write_text(
        json.dumps(fields, ensure_ascii=False), encoding="utf-8")


def test_时间线不给没跑完的任务画终点():
    """**变异判据**：把 `tracker.is_terminal(status)` 换回原来那个硬编码元组
    （含 `decomposed`），本用例必须红 —— 它会给 decomposed 画一个终点节点。"""
    from singularity.scheduler import _api_tasks
    _write_task("tl-decomposed", status="decomposed", route_level="any",
                snapshot_id="snap-1", route_type="default")
    data, code = _api_tasks.task_timeline("tl-decomposed")
    assert code == 200
    ends = [n for n in data["timeline"] if n["to"] in ("done", "failed", "rolled_back")]
    assert ends == [], f"给没跑完的任务画了终点：{ends}"
    last = data["timeline"][-1]
    assert last["to"] == "decomposed", data["timeline"]
    assert last["meta"].get("terminal") is False, f"节点没说清它不是终点：{last}"


def test_时间线对真终态照旧画终点():
    """反向保护：别为了不撒谎把真终态也吞了。"""
    from singularity.scheduler import _api_tasks
    _write_task("tl-done", status="done", route_level="any", snapshot_id="snap-1")
    data, _ = _api_tasks.task_timeline("tl-done")
    assert any(n["to"] == "done" for n in data["timeline"]), data["timeline"]
    assert data["timeline"][-1]["to"] == "done"


def test_is_terminal_和状态机同源():
    """`decomposed` / `conflict_held` 在**状态机那张表**里就不是终态
    —— 时间线要用的是那张表，不是自己抄的一份。"""
    from singularity.scheduler import tracker as T
    assert T.is_terminal("done") and T.is_terminal(T.TaskStatus.FAILED)
    assert not T.is_terminal("decomposed")
    assert not T.is_terminal("conflict_held")
    assert not T.is_terminal("running")
    assert not T.is_terminal("") and not T.is_terminal(None)


# ═══════════════════════════════════════════════════════════════
# ⑤ gate 的文件兜底表 vs 分类器 prompt：两份名单得对得上
# ═══════════════════════════════════════════════════════════════
# `router._CLASSIFY_PROMPT` 里告诉模型"核心引擎文件 = core/tokenizer/graph/search/config.py"，
# 而 `config.GATE_TRIGGER_FILES`（分类挂掉时的**文件级兜底**）里**没有 config.py**
# ⇒ "分类挂 + 只改 config.py"这个窄窗里 gate 真会被跳过。

def test_gate兜底表要认分类器prompt里的核心文件():
    """**从 prompt 里把文件名抠出来比**，不是手打一份期望值 —— 两边任意一侧改了都该红。
    变异：从 `GATE_TRIGGER_FILES` 里去掉 `config.py` → 红。"""
    import re
    from singularity.scheduler import config as C, router
    # prompt 里那句"还要判断是否触及核心引擎文件(需要GATE门禁):"后面那一行文件清单
    tail = router._CLASSIFY_PROMPT.split("需要GATE门禁")[1]
    names = set(re.findall(r"[\w.]+\.py", tail))
    assert names, "prompt 里那句核心文件清单没解析出来（格式变了？）"
    missing = names - set(C.GATE_TRIGGER_FILES)
    assert not missing, f"分类器说这些要 gate、兜底表里没有：{sorted(missing)}"


def test_gate兜底按文件名比_改到核心文件就触发():
    from singularity.scheduler import validator
    assert validator._gate_check_by_files(["src/engine/config.py"]) is True
    assert validator._gate_check_by_files(["a/b/core.py"]) is True
    assert validator._gate_check_by_files(["src/main.py", "README.md"]) is False
    assert validator._gate_check_by_files([]) is False


# ═══════════════════════════════════════════════════════════════
# ⑥ is_model_available：查不动就放行（方向刻意），但**必须出声**
# ═══════════════════════════════════════════════════════════════

def test_模型库查不动时放行但出声(monkeypatch):
    """变异：去掉那句 `witness.warn` → 本用例红（放行的方向是刻意的，出声不是）。

    改成"拒绝"是**错的**：registry 一坏就全库查不到 ⇒ 候选链整条空掉
    ⇒ "所有 agent 均失败"，拿更大的误伤换更难查的故障。
    """
    from singularity.scheduler import api_store, model_registry, witness

    warned: list[str] = []
    monkeypatch.setattr(witness, "warn", lambda scope, msg, key="": warned.append(msg))
    monkeypatch.setattr(api_store, "_quota_dead", lambda: {})

    def _boom(_m):
        raise RuntimeError("registry 坏了")
    monkeypatch.setattr(model_registry, "provider_for_model", _boom)

    assert api_store.is_model_available("某个模型") is True, "方向变了：不该拒绝"
    assert warned and "model_available_lookup_failed" in warned[0], warned


# ═══════════════════════════════════════════════════════════════
# ⑦ route_type 词表：三处不一致（web 6 / router 5 / validator 3）
# ═══════════════════════════════════════════════════════════════
# `web/app.py` 的门口手写了 6 个值（多一个 `fusion`），而 `fusion` **全仓没有生产者**：
# 分类器产不出、下游也没有任何判据认识它 ⇒ 能过门、落库、进统计，
# 而 `validator._annotate_unverified` 对它整段静默不发生。

def test_route_type_门口只收分类器认得的值():
    """走**真的 HTTP 端点**，不是读源码 —— 钉的是"门口到底收不收"。

    变异：把 web 门口改回本地那份含 `fusion` 的手写集合 → 本用例红（`fusion` 会变 200）。
    """
    import singularity.web.app as webapp
    from singularity.scheduler import router
    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()

    r = c.post("/api/tasks", json={"description": "x", "route_type": "fusion"})
    assert r.status_code == 400, f"`fusion` 全仓没有生产者，不该从门口进来: {r.status_code}"
    assert "route_type" in str(r.data)
    # 正向：分类器认得的值得照样能进（别为了拦一个 fusion 把整条门关了）
    ok = c.post("/api/tasks", json={"description": "x", "route_type": "bugfix"})
    assert ok.status_code == 200, f"正常类型被误拦: {ok.status_code} {ok.data[:120]}"
    assert "fusion" not in router.VALID_TASK_TYPES


def test_框架不认识的任务类型要出声():
    """`route_type` 数据流是闭合的 —— 门口放进来的新值都会走到这儿。
    这里一个字不说，就是"少做几件事、不报错"（防御模式 §44）。
    变异：去掉那段 else 分支 → 红。"""
    from singularity.scheduler.validator import ValidationReport, _annotate_unverified
    r = ValidationReport()
    _annotate_unverified(r, "fusion", ["a.py"])
    assert any("框架不认识这个任务类型" in u for u in r.unverified), r.unverified
    # 认识的类型不许被误伤
    for t in ("bugfix", "refactor", "feature", "docs", "default"):
        rr = ValidationReport()
        _annotate_unverified(rr, t, ["a.py"])
        assert not any("框架不认识" in u for u in rr.unverified), (t, rr.unverified)


# ═══════════════════════════════════════════════════════════════
# ⑧ 本机 origin：HTTP 门和 WS 门必须是**同一个判定**
# ═══════════════════════════════════════════════════════════════
# HTTP 门（CSRF 守卫 / CORS 头）原来自己写了一份 `web/app.py:_is_local_origin`，
# WS 门用的是 `_auth` 里另一个正则 —— 两份内容不同（这份有 `0.0.0.0`、
# `urlparse` 还会把主机名转小写；那份没有、且大小写敏感）。
# 而"挡任意网页删任务"这条路**只靠 Origin 校验**（`QIDIAN_AUTH` 默认关）。

# 两端都要判成"本机"、和"外来"的样本。`websockets` 用的是 **fullmatch**
# （`server.py:340-345`），所以这里也按 fullmatch 比 —— 用 `match` 比会放过后缀绕行。
_ORIGIN_ACCEPT = [
    "http://localhost:5050", "http://127.0.0.1:8765", "https://127.0.0.1",
    "http://0.0.0.0:5050", "http://[::1]:8765", "https://LOCALHOST:5050",
]
_ORIGIN_REJECT = [
    "http://evil.com", "http://127.0.0.1.evil.com", "http://localhost.evil.com",
    "http://192.168.1.5:5050", "http://127.0.0.1evil.com", "",
]


def test_http门和ws门对本机来源的判定必须一致():
    """**变异判据**：把 WS 正则改回手写那份（没有 `0.0.0.0`、大小写敏感）→ 红；
    把 HTTP 判定改回本地那份 → 也不会红（值一样）—— 所以钉的是"同一份"这件事，
    靠的是两边**逐条样本比**，不是比实现。"""
    from singularity.scheduler import _auth
    assert _auth.LOCAL_HOSTNAMES, "主机名表空了"

    def ws_accepts(origin: str) -> bool:
        # 复刻 websockets 的判定：精确串相等 或 正则 fullmatch
        return any(o == origin if not hasattr(o, "fullmatch")
                   else (origin is not None and o.fullmatch(origin) is not None)
                   for o in _auth.ws_allowed_origins() if o is not None)

    for o in _ORIGIN_ACCEPT:
        assert _auth.is_local_origin(o) is True, f"HTTP 门没认 {o}"
        assert ws_accepts(o) is True, f"WS 门没认 {o} —— 两个门判定不同"
    for o in _ORIGIN_REJECT:
        assert _auth.is_local_origin(o) is False, f"HTTP 门误放 {o!r}"
        assert ws_accepts(o) is False, f"WS 门误放 {o!r}"


def test_畸形_origin_头不放行也不静默(monkeypatch):
    """`Origin` 是**外部可控**的头，`urlparse` 对畸形 IPv6 会抛 —— 抛了不能 500、
    也不能悄悄当成"没带 Origin"（那样日志里两种事长得一样）。
    变异：去掉那句 `witness.warn` → 棘轮先红（静默 except）；去掉 `return False`
    改成 `return True` → 本用例红。"""
    from singularity.scheduler import _auth, witness
    warned: list[str] = []
    monkeypatch.setattr(witness, "warn", lambda scope, msg, key="": warned.append(msg))
    assert _auth.is_local_origin("http://[::1") is False
    assert warned and "bad_origin_header" in warned[0], warned


def test_http门用的就是那一份判定():
    """**同一个函数对象**，不是"另写一个行为一样的" —— 行为一样的两份明天就会漂，
    而两边各写一份正是这个 bug 本身。变异：把 app.py 那行 import 换成任何本地实现 → 红。"""
    from singularity.scheduler import _auth
    import singularity.web.app as webapp
    assert webapp._is_local_origin is _auth.is_local_origin, \
        "HTTP 门又有了自己的第二份实现"
    assert _auth.is_local_origin(None) is False   # 非字符串不许抛


# ═══════════════════════════════════════════════════════════════
# ⑨ worker 异常分支的抢救段原来裸着（超时那条包了兜底）
# ═══════════════════════════════════════════════════════════════
# 两条路做的是**同一件事**（抢救已知事实再落 trace），却只给超时那条包了
# `_strand_guard`。worker 异常那条裸着 ⇒ 抢救里任何一步抛都会**中断整个 reap 循环**，
# 后面已经完成的 future 这一轮不再处理。

class _BoomFuture:
    """`done()` 为真、`result()` 抛 —— 就是 worker 异常那一支。"""
    def done(self):
        return True

    def result(self):
        raise RuntimeError("worker 炸了")

    def cancel(self):
        return True


def test_worker异常分支的抢救段不许带崩回收循环(monkeypatch, tmp_path):
    """**变异判据**：把新加的 try/except 去掉（回到裸着的三行）→ 本用例红
    （异常穿出去，第二个 future 这轮收不到 ⇒ `results` 只有 1 条）。

    两个 future **都是** worker 异常：`results` 的长度就是"循环走完了没有"的判据。
    """
    from singularity.scheduler import orchestrator as orch

    monkeypatch.setattr(orch, "wait", lambda *a, **k: None)          # 别白等 10s
    monkeypatch.setattr(orch.config, "CANCEL_DIR", tmp_path)
    monkeypatch.setattr(orch.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(orch.tracker, "transition", lambda *a, **k: None)
    monkeypatch.setattr(orch, "cleanup_task_artifacts", lambda *a, **k: 0)
    monkeypatch.setattr(orch, "_save_trace", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_account_salvaged", lambda *a, **k: None)

    def _boom(*a, **k):
        raise RuntimeError("抢救自己炸了")
    monkeypatch.setattr(orch, "_salvage_timed_out", _boom)

    warns: list[str] = []
    monkeypatch.setattr(orch.witness, "warn",
                        lambda scope, msg, key="": warns.append(f"{key}|{msg}"))

    def _t(i):
        return type("T", (), {"id": f"t{i}", "description": "x", "depends_on": [],
                              "created_at": 1.0, "route_type": "default"})()

    running = {_BoomFuture(): (_t(1), None, None, None, 0.0),
               _BoomFuture(): (_t(2), None, None, None, 0.0)}
    results: list = []
    orch._reap_futures(running, {}, None, None, results)

    assert len(results) == 2, f"循环被抢救段的异常打断了，只收了 {len(results)} 个"
    assert any("salvage_worker_error" in w for w in warns), f"抢救失败没出声：{warns}"


# ═══════════════════════════════════════════════════════════════
# ⑩ 三条"静默错"（外派 ⑩ 抓到）
# ═══════════════════════════════════════════════════════════════

def test_task_list_的_level_filter_比的是_route_level():
    """公开 API 的 `?level=` 原来比的是 `route_type` —— **错得静默**（不报错、
    返回一批看起来合理的任务）。变异：把 `route_level` 改回 `route_type` → 红。"""
    from singularity.scheduler import _api_tasks
    all_tasks = [
        {"id": "a", "_filename": "a.json", "route_level": "ops", "route_type": "bugfix"},
        {"id": "b", "_filename": "b.json", "route_level": "any", "route_type": "ops"},
    ]
    orig = _api_tasks._list_all_tasks
    _api_tasks._list_all_tasks = lambda: all_tasks
    try:
        got = [t["id"] for t in _api_tasks.task_list(level_filter="ops")[0]["tasks"]]
    finally:
        _api_tasks._list_all_tasks = orig
    assert got == ["a"], f"按 level 过滤拿到了 {got}（那是按 type 过滤的结果）"


def test_roles_端点拒写时把原因透出来(monkeypatch, tmp_path):
    """`_write_role_override` 读不动坏文件时**拒绝写**（顺序对），但三个 roles 端点
    原来都不接 ⇒ 500 ⇒ 前端只看到 generic 错误，中文原因丢了。
    变异：把 try/except 去掉 → 异常穿出去 → 本用例红（Flask 测试客户端会抛，
    或返回 500 而不是 503）。"""
    import singularity.web.app as webapp
    from singularity.scheduler import config as sched_config

    monkeypatch.setattr(sched_config, "QIDIAN_DIR", tmp_path)
    bad = tmp_path / "roles_custom.json"
    bad.write_text("{ 这不是 JSON", encoding="utf-8")   # 读不动 ⇒ 必须拒写

    webapp.app.config["TESTING"] = True
    c = webapp.app.test_client()
    r = c.patch("/api/roles/implementer", json={"label": "x"})
    assert r.status_code == 503, f"应 503（带原因的拒绝），实际 {r.status_code}"
    body = r.get_json()
    assert "拒绝写入" in body["error"] and "读不动" in body["error"], body
    assert bad.read_text(encoding="utf-8") == "{ 这不是 JSON", "拒写了还是把文件覆盖了"


def test_project_lineage_返回项目自己的血缘():
    """这个端点叫 lineage，原来拿 project_id 去**任务模板表**里查
    （模板名是 bugfix/feature 这种，项目 id 是数字）⇒ 恒不命中 ⇒ 永远返回
    "这个项目的任务列表"，而项目自己那份 `ProjectState.lineage` **没人读**。
    变异：改回模板表那支 → 红。"""
    from singularity.scheduler import _api_projects
    from singularity.scheduler import project as proj_mod
    # 真项目：造一个再读
    p = proj_mod.create("血缘测试项目")
    # `add_lineage` 是**纯内存追加**（落盘由调用方负责），所以这里要自己 save
    p.add_lineage({"action": "phase", "from": "planning", "to": "executing"})
    proj_mod.save(p)
    data, code = _api_projects.project_lineage(p.id)
    assert code == 200
    assert data["lineage"] and data["lineage"][-1]["action"] == "phase", data
    # 不存在的项目：明说，而不是回一份看起来合理的东西
    miss, mc = _api_projects.project_lineage("9900000000001")
    assert mc == 404 and "不存在" in miss["error"], (mc, miss)


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
