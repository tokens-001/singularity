"""第二批修复（2026-09-14）—— 每处都钉在"删掉那一行它会红"上。

来源：`docs/扫bug-03-20260914.md`（A 轮）+ `docs/扫bug-02-20260914.md`（#6 npm 豁免）
     + 外派「改动审阅」`~/Desktop/ZCode审阅/今晚改动-01.answer.md`（#1 执行器起表 / 其它执行器不吃预算）。

⚠️ 两处**故意没测**，理由写在各自修复注释里：
  · `web/app.py` 的 secret_key 是模块级代码，测它要重导整个 app 模块；
  · `_norm_verdict(default="accepted")` 那处注释自认是已知残留、要改先有测量（本轮不动）。
"""
import ast
import json
import os
import tempfile
import time
from pathlib import Path

import pytest


# ═══════════════════════════════════════════════════════════════
# ① validator.run_project_tests —— npm 不再豁免
# ═══════════════════════════════════════════════════════════════
# 原来那句是 `if r.returncode != 0 and name != "npm":` —— npm 非零退出时
# 两个分支都不进，`passed` 停在初值 True、`runner` 停在 ""，最后被写成
# "no test runner found（三个都启动不了）"。**纯 JS 项目里 py/unittest 必然
# "没找到测试"，只剩 npm 这一票** ⇒ 全红被报成"没跑"。

def _mk(files: dict) -> str:
    d = tempfile.mkdtemp()
    for name, content in files.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(content)
    return d


def test_npm_测试挂了要算失败():
    from singularity.scheduler.validator import run_project_tests
    d = _mk({"package.json": json.dumps(
        {"name": "x", "version": "1.0.0",
         "scripts": {"test": 'node -e "process.exit(1)"'}})})
    r = run_project_tests(cwd=d)
    assert r["passed"] is False, "npm 全红却报 passed —— 这正是原来那条豁免的后果"
    assert r["runner"] == "npm", f"runner 停在 {r['runner']!r}（空串=谎报'三个都启动不了'）"


def test_没配_test_脚本不算失败():
    """**反向保护**：没配 test 脚本是"没找到测试"，不是"测试挂了"。

    少了这条，任何一个带 package.json 但没有测试脚本的仓库都会被判失败 ——
    那是把一个 fail-open 换成 fail-closed 过头。
    """
    from singularity.scheduler.validator import run_project_tests
    d = _mk({"package.json": json.dumps({"name": "x", "version": "1.0.0"})})
    r = run_project_tests(cwd=d)
    assert r["passed"] is True
    assert r["runner"] == "none"
    assert "没找到测试" in r["output"]


def test_没有_package_json的纯_python_项目不受影响():
    """npm 的 ENOENT 也不能被当成失败 —— 全仓大多数项目没有 package.json。"""
    from singularity.scheduler.validator import run_project_tests
    d = _mk({"test_ok.py": "def test_ok(): assert True"})
    r = run_project_tests(cwd=d)
    assert r["passed"] is True and r["runner"] == "pytest"


def test_空目录仍然是没找到测试():
    """回归：别把"三个都跑了但都没测试"改成了"失败"。"""
    from singularity.scheduler.validator import run_project_tests
    r = run_project_tests(cwd=_mk({}))
    assert r["passed"] is True and r["runner"] == "none"
    assert "没找到测试" in r["output"]


# ═══════════════════════════════════════════════════════════════
# ② /api/auth/status —— 免认证白名单里不再泄漏用户清单
# ═══════════════════════════════════════════════════════════════

def test_auth_status_默认不吐用户清单(monkeypatch):
    """默认值必须是 False（fail-closed）—— 靠"每次记得传"迟早漏。"""
    from singularity.scheduler import _auth, _api_admin
    monkeypatch.setattr(_auth, "get_auth",
                        lambda: type("S", (), {"list_users": lambda s: [{"id": "admin"}]})())
    data, code = _api_admin.auth_status()
    assert code == 200
    assert "users" not in data, "默认就把用户清单吐出来了 —— 白名单端点绕过了自己"
    assert data["enabled"] is False


def test_auth_status_传了才吐(monkeypatch):
    """对照：认证过的调用方仍然拿得到清单（不然这个字段就白删了）。"""
    from singularity.scheduler import _auth, _api_admin
    monkeypatch.setattr(_auth, "get_auth",
                        lambda: type("S", (), {"list_users": lambda s: [{"id": "admin"}]})())
    data, _ = _api_admin.auth_status(include_users=True)
    assert data["users"] == [{"id": "admin"}]


def test_auth_status_路由_未认证不回清单(monkeypatch):
    """接线：路由必须**自己判身份**再决定传不传 —— 这个端点在公开白名单里，
    `_guard_auth` 不会给它注入 `g.auth_user`。"""
    from singularity.web import app as W
    monkeypatch.setattr(W, "_AUTH_ENABLED", True)
    with W.app.test_request_context("/api/auth/status"):   # 不带 Authorization 头
        resp, code = W.api_auth_status()      # 直接调=拿原始元组（Flask 只在路由层拆它）
    data = resp.get_json()
    assert code == 200
    assert "users" not in data, "开了认证、没带 token，却把全量用户清单给出去了"
    # ⚠️ 不在这里断言 `data["enabled"]` —— 它是 handler 里**另读一次环境变量**得到的
    # （`os.environ.get("QIDIAN_AUTH")`），跟本测试打的 `app._AUTH_ENABLED` 是两份来源。
    # 只钉本处要钉的那件事：**身份没证 → 清单不给**。


# ═══════════════════════════════════════════════════════════════
# ③ /api/auth/bootstrap —— 回完整 token（原来只回 `token[:8]`）
# ═══════════════════════════════════════════════════════════════
# 盘上只存哈希、`to_dict()` 不含明文 ⇒ 这里是**唯一**一次能看到完整 token 的机会。
# 打前缀 = 给一个没法用的 token ⇒ 一开 QIDIAN_AUTH 全员 401 且无自助恢复通道。

class _AdminStub:
    id = "admin"
    token = "0123456789abcdef0123456789abcdef"

    def to_dict(self):
        return {"id": "admin", "role": "admin"}      # 注意：**不含 token**


def test_bootstrap_要回完整_token(monkeypatch):
    from singularity.scheduler import _auth, _api_admin
    monkeypatch.setattr(_auth, "get_auth",
                        lambda: type("S", (), {"_users": {},
                                               "bootstrap": lambda s: _AdminStub()})())
    data, code = _api_admin.auth_bootstrap()
    assert code == 200
    assert data["token"] == _AdminStub.token, "只回了前缀 —— 等于没给（to_dict 里也没有）"
    assert _AdminStub.token[:8] not in data.get("message", ""), \
        "message 里又摆了一遍前缀，读者会把那个当 token"


# ═══════════════════════════════════════════════════════════════
# ④ MCP 删服务器 —— 注册表必须一起摘（原来只删配置文件）
# ═══════════════════════════════════════════════════════════════
# `get_registry()` 是**全局单例**：只 `save_mcp_configs` 的话，`_clients/_tools`
# 里那个服务器的客户端和工具原样还在 ⇒ 下次装配 agent 时旧工具照旧回来。

class _Cfg:
    def __init__(self, name):
        self.name = name


class _FakeClient:
    def __init__(self, name):
        self.cfg = _Cfg(name)
        self.disconnected = False

    def disconnect(self):
        self.disconnected = True


class _FakeTool:
    def __init__(self, server):
        self.server_name = server


def _registry_with(monkeypatch):
    from singularity.scheduler import mcp as M
    reg = M.MCPRegistry()
    dead, alive = _FakeClient("dead"), _FakeClient("alive")
    reg._clients = {"dead": dead, "alive": alive}
    reg._tools = [_FakeTool("dead"), _FakeTool("alive")]
    reg._tool_index = {"t_dead": dead, "t_alive": alive}
    saved = {}
    monkeypatch.setattr(M, "load_mcp_configs",
                        lambda: [_Cfg("dead"), _Cfg("alive")])
    monkeypatch.setattr(M, "save_mcp_configs",
                        lambda cs: saved.update(names=[c.name for c in cs]))
    monkeypatch.setattr(M, "get_registry", lambda: reg)
    return reg, dead, saved


def test_删_MCP_服务器要把它从注册表摘掉(monkeypatch):
    from singularity.scheduler import _api_admin
    reg, dead, saved = _registry_with(monkeypatch)

    data, code = _api_admin.mcp_server_delete("dead")

    assert code == 200
    assert saved["names"] == ["alive"], "配置文件那半没删对"
    assert "dead" not in reg._clients, "界面上删了、注册表里还在 ⇒ 模型还能调它"
    assert [t.server_name for t in reg._tools] == ["alive"]
    assert "t_dead" not in reg._tool_index
    assert "t_alive" in reg._tool_index, "把无关服务器的工具一起摘了"
    assert dead.disconnected, "客户端没断开 —— 子进程/连接泄漏"


def test_重连_MCP_必须喂全量配置(monkeypatch):
    """⚠️ **这条的判据 09-14 改过**（原来断言 `loaded == [["dead"]]`，那是错的）。

    `MCPRegistry.load_configs` 是**整体原子换入**（`mcp.py:335` 直接把
    `self._clients/_tools/_tool_index` 换成按入参重建的那一份），**不是增量加载**。
    所以只喂被重连的那一个 ⇒ **其余服务器连同工具一起从注册表里消失**，
    要等手动 refresh / 重启才回来 —— 跟 DELETE 那条要治的"删了还在"正好反方向，
    而且更坏（静默地把好的也拿掉了）。
    """
    from singularity.scheduler import _api_admin
    reg, _dead, _ = _registry_with(monkeypatch)
    loaded = []
    monkeypatch.setattr(type(reg), "load_configs",
                        lambda self, cfgs: loaded.append([c.name for c in cfgs]))
    data, code = _api_admin.mcp_server_reconnect("dead")
    assert code == 200
    assert loaded == [["dead", "alive"]], f"只喂了被重连的那个 ⇒ 别的服务器被抹了：{loaded}"


def test_加_MCP_服务器要同步进注册表(monkeypatch):
    """add 和 delete 是同一面镜子 —— delete 09-14 修了，add 当时漏了。

    不喂注册表 ⇒ 接口回 `{"ok": True}` 而模型侧工具（来自 `_dispatch_skills` 读的
    那个单例）一个新都没有，要等手动 refresh / 重启。**加成功了却调不到**。
    """
    from singularity.scheduler import _api_admin
    reg, _dead, _saved = _registry_with(monkeypatch)
    loaded = []
    monkeypatch.setattr(type(reg), "load_configs",
                        lambda self, cfgs: loaded.append([c.name for c in cfgs]))

    data, code = _api_admin.mcp_server_add({"name": "newone", "command": "npx x"})

    assert code == 200
    assert loaded == [["dead", "alive", "newone"]], f"配置写了但注册表没喂 ⇒ 模型调不到：{loaded}"


# ═══════════════════════════════════════════════════════════════
# ⑤ SSE 回放 —— 取快照必须在锁里
# ═══════════════════════════════════════════════════════════════
# ⚠️ 为什么用 AST 钉而不是"跑一遍看炸不炸"：实测裸迭代 deque 要 **40 万次才炸 ~70 次**
# （约 1/5700）—— 跑一遍几乎必然绿，那种测试只给虚假安全感。
# 而改回 `list(_sse_event_buffer)` 也不行：`list()` 走的仍是 `deque.__iter__`
# （子类覆盖 `__iter__` 会被调用，验证过），只是 CPython 的 C 层 `list_extend`
# 那一段碰巧不让出 GIL ⇒ **靠实现细节，不是保证**。所以钉"两头都上锁"这个结构。

def _locked_with_bodies(tree) -> list[ast.AST]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            e = item.context_expr
            if isinstance(e, ast.Name) and e.id == "_SSE_BUF_LOCK":
                out.append(node)
    return out


def _touches_sse_buffer(node) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "_sse_event_buffer":
            return True
    return False


def test_SSE_缓冲区的读和写都在同一把锁里():
    from singularity.web import app as W
    tree = ast.parse(Path(W.__file__).read_text(encoding="utf-8"))

    locked = _locked_with_bodies(tree)
    assert locked, "找不到 `with _SSE_BUF_LOCK:` —— 锁被删了"

    # 写法：锁块里既要有 `list(_sse_event_buffer)`（回放侧取快照），
    # 也要有 `_sse_event_buffer.append(...)`（广播侧写入）。
    def _calls_builtin_list(node) -> bool:
        return any(isinstance(s, ast.Call) and isinstance(s.func, ast.Name)
                   and s.func.id == "list" for s in ast.walk(node))

    def _calls_append(node) -> bool:
        return any(isinstance(s, ast.Call) and isinstance(s.func, ast.Attribute)
                   and s.func.attr == "append" for s in ast.walk(node))

    assert any(_touches_sse_buffer(b) and _calls_builtin_list(b) for b in locked), \
        "回放侧没在锁里取快照 —— 广播线程一 append 就 RuntimeError，而外层 try 只有 finally"
    assert any(_touches_sse_buffer(b) and _calls_append(b) for b in locked), \
        "广播侧 append 没在锁里 —— 只锁一头等于没锁"

    # 反向：任何**裸迭代** `_sse_event_buffer` 的地方都不许有
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and _touches_sse_buffer(node.iter):
            pytest.fail("直接迭代 `_sse_event_buffer` —— 必须迭代锁里取的快照")


# ═══════════════════════════════════════════════════════════════
# ⑥ fallback 链：每次尝试用**剩下**的预算，不是把同一份重置一遍
# ═══════════════════════════════════════════════════════════════
# `budget_s` 是调用方按任务死线倒推的"现在还能花多少秒"。链上最多 3 次尝试，
# 每轮都传原值 = 每轮把预算重置 ⇒ 最坏 3× 超时（§67 那个病又下了一层）。

def test_fallback_链每次尝试用剩下的预算(monkeypatch):
    from singularity.scheduler import _dispatch_exec as pd
    from singularity.scheduler.executors.base import ExecutorResult

    seen = []

    def _fake_run(*a, **k):
        seen.append(k.get("budget_s"))
        time.sleep(0.05)
        return ExecutorResult(success=False, raw_output="",
                              error="模型吐了个空", error_kind="exec")

    monkeypatch.setattr(pd, "pick_agent_fallback_chain",
                        lambda *a, **k: [{"model": f"m{i}", "type": "openai-agent"}
                                         for i in range(3)])
    monkeypatch.setattr(pd, "_prefer_by_strengths", lambda task, chain: chain)
    monkeypatch.setattr(pd, "_committee_allowed", lambda *a, **k: False)
    monkeypatch.setattr(pd, "_ensure_agent_type", lambda c: c)
    monkeypatch.setattr(pd, "_model_breaker",
                        type("B", (), {"record_failure": lambda *a: None,
                                       "record_success": lambda *a: None})())
    monkeypatch.setattr(pd, "_run_executor", _fake_run)

    with pytest.raises(RuntimeError):
        pd.dispatch("任务", "any", "tid", {}, budget_s=10.0)

    assert len(seen) == 3, f"链没走满 3 次：{seen}"
    assert all(b is not None for b in seen)
    assert seen[0] > seen[1] > seen[2], (
        f"三次拿到的是同一份预算 ⇒ 每轮重置、最坏 3× 超时：{seen}")


def test_没给预算时链上仍然是_None(monkeypatch):
    """对照：`budget_s=None`（阶段级那条路）必须**保持 None**，不许变成 0 ——
    0 会让执行器第 1 轮就收尾、一个调用都不发。"""
    from singularity.scheduler import _dispatch_exec as pd
    from singularity.scheduler.executors.base import ExecutorResult
    seen = []
    monkeypatch.setattr(pd, "pick_agent_fallback_chain",
                        lambda *a, **k: [{"model": "m", "type": "openai-agent"}])
    monkeypatch.setattr(pd, "_prefer_by_strengths", lambda task, chain: chain)
    monkeypatch.setattr(pd, "_committee_allowed", lambda *a, **k: False)
    monkeypatch.setattr(pd, "_ensure_agent_type", lambda c: c)
    monkeypatch.setattr(pd, "_model_breaker",
                        type("B", (), {"record_failure": lambda *a: None,
                                       "record_success": lambda *a: None})())
    monkeypatch.setattr(pd, "_run_executor",
                        lambda *a, **k: (seen.append(k.get("budget_s")),
                                         ExecutorResult(success=False, raw_output="",
                                                        error="空", error_kind="exec"))[1])
    with pytest.raises(RuntimeError):
        pd.dispatch("任务", "any", "tid", {})
    assert seen == [None], f"None 被换成了 {seen} —— 执行器会立刻收尾"


# ═══════════════════════════════════════════════════════════════
# ⑦ 非 openai_agent 的执行器也要吃预算
# ═══════════════════════════════════════════════════════════════
# 执行器是**每次 dispatch 新建**的，只有 openai_agent 消费 budget_s 的话，
# claude-cli（默认类型！）/ anthropic-api / zhipu-api 三条路照样越过任务死线，
# 被外面那把 900s 的刀无声收割 —— §67 白修一半。

class _P:
    returncode = 0
    stdout = "ok"
    stderr = ""


def _patch_cli_subprocess(monkeypatch) -> list:
    """记下**每一次** `subprocess.run` 的 timeout。

    ⚠️ 必须记列表不是单个值：`run()` 之后还有两次 `subprocess.run`（git diff / ls-files，
    不带 timeout）—— 记单值会被后面那两次覆盖成 None，测试就变成假绿。
    """
    import singularity.scheduler.executors.claude_cli as cc
    calls = []
    monkeypatch.setattr(cc.subprocess, "run",
                        lambda argv, **kw: (calls.append((argv, kw.get("timeout"))), _P())[1])
    return calls


def test_claude_cli_把子进程超时压到预算以内(monkeypatch):
    from singularity.scheduler.executors.claude_cli import ClaudeCliExecutor
    calls = _patch_cli_subprocess(monkeypatch)
    ex = ClaudeCliExecutor({"entry": "claude -p {prompt}"}, "任务", "tid")
    ex.budget_s = 20.0
    ex.run()
    assert calls, "压根没起子进程"
    assert calls[0][1] == 20.0, f"没吃预算，主调用的超时还是 {calls[0][1]}"


def test_claude_cli_没给预算时用原来的上限(monkeypatch):
    from singularity.scheduler.executors.claude_cli import ClaudeCliExecutor
    from singularity.scheduler import config
    calls = _patch_cli_subprocess(monkeypatch)
    ex = ClaudeCliExecutor({"entry": "claude -p {prompt}"}, "任务", "tid")
    ex.run()                       # budget_s 默认 None
    assert calls[0][1] == config.CLAUDE_CLI_TIMEOUT
