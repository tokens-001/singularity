"""第四批修复（2026-09-14 深夜）—— 五处，全是"同一件事只修了一半"那个形状。

出处：外派 `K` 的条3/条4/条5/条6（`复核核实-01`，我核过 6 条全真）、
`E'` 的 `反审-02`、`H` 的 `判清单-01` 反7 —— 三份独立来路指向同几处。

  ① `base._BLOCKED_PATTERNS` 漏 `id_rsa` 一族 / `.netrc` / `.flaskenv` / `.crt` / `.ssh/`
     —— 而 `web/app.py:1358` 那张表**早就补了**、注释还点名了这个洞
  ② 模块级 `_run_command` 的 `subprocess.run` 不传 `env=` ⇒ 子进程继承全量 `os.environ`
     —— anthropic 执行器直接把 `run_command` 派到它，全仓只此一条路不脱敏
  ③ `mcp_server_add` 只写配置、不喂注册表（delete 09-14 修了，add 漏了）
  ④ `mcp_server_reconnect` 喂的是**单个** config，而 `load_configs` 是整体替换
     ⇒ 重连一个把别的全抹了
  ⑤ anthropic `_tmo` 循环外算一次、多轮共用 ⇒ 最坏 max_turns 倍预算
  ⑥ `_io.atomic_write_json` 的 tmp 不带 pid、写入不拿锁（外派 J 审 `防御模式.md` §46 抓到：
     那条的修法只落在 `project.py:479`，这个共用入口没跟着改）

测试都**钉接线**：删掉对应那一行判据（或把参数改回去），测试必须红。
"""
import pytest


# ═══════════════════════════════════════════════════════════════
# ① agent 侧读文件黑名单
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("path", [
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "/home/u/.ssh/id_rsa", "keys/id_rsa",
    ".netrc", "~/.netrc", ".flaskenv",
    "server.crt", "ca.crt",
    ".ssh/known_hosts",
])
def test_无后缀私钥那族要被拦(path):
    """`*.key`/`*.pem` 罩不住 `id_rsa` —— 模型一条 read_file 就能把私钥带走。

    这是"同一件事两半"：`web/app.py:1358` 的 `_SENSITIVE_FILES` 早就有这几个，
    agent 侧三执行器共用的这张表没跟着改（它的注释自己写着"统一入口"）。
    """
    from singularity.scheduler.executors.base import is_blocked_path
    blocked, reason = is_blocked_path(path)
    assert blocked, f"{path!r} 没被拦 —— 私钥能整读进上下文"


@pytest.mark.parametrize("path", [
    "src/main.py", "README.md", "tests/test_ok.py", "docs/notes.md",
])
def test_正常文件别误伤(path):
    """对照：别把闸门修成"什么都读不了"。"""
    from singularity.scheduler.executors.base import is_blocked_path
    blocked, _ = is_blocked_path(path)
    assert not blocked, f"{path!r} 被误拦了"


# ═══════════════════════════════════════════════════════════════
# ② 模块级 `_run_command` 必须脱敏环境变量
# ═══════════════════════════════════════════════════════════════

def test_模块级_run_command_要脱敏_env(monkeypatch, tmp_path):
    """真跑一条 `env`：敏感变量不许出现在子进程的输出里。

    不脱敏的后果：anthropic 执行器把 `run_command` 派到这个模块级函数
    （类方法 `_tool_run` 有 `env=safe_env`、这条路没有）⇒ 模型一条 `env`
    就能读走 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`。
    """
    from singularity.scheduler.executors.openai_agent import _run_command
    monkeypatch.setenv("MY_TEST_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MY_TEST_PLAIN", "visible-ok")

    out = _run_command({"command": "env"}, str(tmp_path))

    assert "sk-should-not-leak" not in out, f"敏感变量漏给子进程了：{out[:400]}"
    assert "visible-ok" in out, "把正常变量也滤掉了 —— 过度过滤会把模型跑的命令弄坏"


# ═══════════════════════════════════════════════════════════════
# ⑤ anthropic：每轮按剩余预算封顶
# ═══════════════════════════════════════════════════════════════

def test_anthropic_每轮都要重算超时(monkeypatch):
    """`_tmo` 必须在**循环内**按剩余预算重算，不能循环外算一次共用。

    循环外算一次 ⇒ 最坏 `max_turns`（默认 10）倍预算，照样撞穿任务死线
    被外面那把 900s 的刀无声收割。zhipu / openai_agent 都逐轮看表，只有这份没防。
    """
    from singularity.scheduler.executors import anthropic_api as A

    seen = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            # 每轮都返回一个 tool_use ⇒ 循环会继续到下一轮
            return {"usage": {}, "content": [
                {"type": "tool_use", "id": "t1", "name": "read_file",
                 "input": {"path": "x"}}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.append(timeout)
        return _Resp()

    # `httpx` 是在 run() 里 `import httpx` 的局部导入 ⇒ 只能打全局模块
    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    ex = A.AnthropicApiExecutor(agent_cfg={}, task="t", task_id="tid", cwd="/tmp",
                          skill_tools=[], mcp_tools=[], skill_prompt="")
    ex.budget_s = 30.0
    monkeypatch.setattr(ex, "_execute_tool", lambda name, args: "ok")
    monkeypatch.setattr(ex, "_get_changed_files", lambda: [])

    ex.run()

    assert len(seen) >= 2, f"没跑到第二轮，测不到递减：{seen}"
    # 每轮的超时都必须 ≤ 预算；且后面那轮要**比第一轮小**（时间真的在走）
    assert all(t <= 30.0 for t in seen), f"某轮超时越过了预算：{seen}"
    assert seen[1] < seen[0], f"第二轮没重算剩余预算，还在用第一轮的值：{seen}"


# ═══════════════════════════════════════════════════════════════
# ⑥ Observer WS 的默认绑定必须是回环（扫bug-02④ 的另一半）
# ═══════════════════════════════════════════════════════════════

def test_observer_默认绑回环不许对外():
    """`observer/server.py` 全文没有任何鉴权判定 ⇒ 默认绑 `0.0.0.0` 是 fail-open。

    ⚠️ **生产路径本来就是对的**（`web/app.py:2289` 显式传 `host="127.0.0.1"`），
    所以要钉的**不是"线上有没有在漏"**，而是**默认值往哪一侧倒** ——
    下一个省略 `host=` 的调用方直接就把一个无鉴权入口挂到公网上了。
    同族：batch2 那条 `/api/auth/status` 默认不许吐用户清单（fail-closed 默认值）。
    """
    import inspect

    from singularity.observer import config as OC
    from singularity.scheduler import bridge as B

    assert OC.DEFAULT_HOST == "127.0.0.1", \
        f"observer 的默认绑定位被改回对外了：{OC.DEFAULT_HOST}"
    default_host = inspect.signature(B.start_observer_server).parameters["host"].default
    assert default_host == "127.0.0.1", \
        f"start_observer_server 的默认 host 被改回对外了：{default_host}"


# ═══════════════════════════════════════════════════════════════
# ⑧ WS 的 Origin 校验（真起服务、真连一次）
# ═══════════════════════════════════════════════════════════════
# 不写成"源码里有没有 origins=" 那种形状测试 —— 那种测的是我改没改，不是**门拦没拦住**。

def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _run_ws_probe(port: int, origin) -> tuple[bool, str]:
    """起一个真的 ObserverServer，用一个真的 WS 客户端去连。

    返回 (连上了吗, 明细)。**不带 origin 就不带 Origin 头** ——
    非浏览器客户端（websocat / 脚本）正是这个形状。
    """
    import asyncio
    import websockets

    async def main() -> tuple[bool, str]:
        from singularity.observer.server import ObserverServer
        srv = ObserverServer(host="127.0.0.1", port=port)
        await srv.start()
        try:
            kw = {}
            if origin is not None:
                kw["additional_headers"] = {"Origin": origin}
            try:
                # `proxy=None`：本机跑测试时 websockets 会按**系统代理设置**自动挑代理
                # （这台机器上是 Clash），于是连 127.0.0.1 也走 SOCKS —— 实测报
                # `ImportError: connecting through a SOCKS proxy requires python-socks`。
                # 连回环不需要代理，显式关掉。
                async with websockets.connect(
                        f"ws://127.0.0.1:{port}", open_timeout=5, proxy=None, **kw) as ws:
                    first = await asyncio.wait_for(ws.recv(), timeout=5)
                    return True, str(first)[:120]
            except Exception as e:              # noqa: BLE001
                return False, f"{type(e).__name__}: {e}"
        finally:
            await srv.stop()

    return asyncio.run(main())


def test_外部网页的_Origin_连不上_observer_ws():
    """**正题**：一个从 evil.com 打开的网页不许连上来。

    它连上就能发 `{"action":"chat"}`，而观察者的工具箱里有
    `create_task` / `delete_task` / `delete_failed_tasks` / `control_loop`
    ⇒ 随手打开的一个网页就能删任务、停调度循环。回环绑定挡不住这条路
    （WS 不受 CORS 预检限制，浏览器照发），`QIDIAN_AUTH` 默认又关着
    ⇒ **Origin 校验是唯一那道门**。
    """
    ok, detail = _run_ws_probe(_free_port(), "http://evil.example.com")
    assert not ok, f"外部 Origin 连上了 —— 门没拦住：{detail}"


def test_本机_UI_的_Origin_连得上(monkeypatch):
    """对照：本机 UI（`app.py:2306` 起在 127.0.0.1:5050）必须连得上，别把门焊死。"""
    monkeypatch.setenv("QIDIAN_SKIP_EMBED", "1")
    ok, detail = _run_ws_probe(_free_port(), "http://127.0.0.1:5050")
    assert ok, f"本机 UI 被自己的门拦住了：{detail}"


def test_不带_Origin_的客户端连得上():
    """对照：websocat / 脚本这类**不带 Origin 头**的客户端要放行。

    `websockets` 的判定是 `for ... == origin ... else: raise InvalidOrigin`
    （`server.py:339-350`）—— **不显式把 `None` 放进允许列表，这些客户端会被一起拒掉**。
    而它们不是这条攻击的载体（浏览器一定带 Origin）。
    """
    ok, detail = _run_ws_probe(_free_port(), None)
    assert ok, f"不带 Origin 的客户端被误杀：{detail}"


def _run_ws_probe_on(port: int, origin):
    """纯客户端探针（服务已经在跑）—— 不启服务，只连。"""
    import asyncio
    import websockets

    async def main():
        kw = {}
        if origin is not None:
            kw["additional_headers"] = {"Origin": origin}
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}",
                                          open_timeout=5, proxy=None, **kw) as ws:
                return True, "connected"
        except Exception as e:              # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    return asyncio.run(main())


def test_bridge_ws_也要校验_Origin():
    """同一个洞的另一半：`bridge.start_ws_server`（5051）原来也没传 `origins=`。

    ⚠️ 这条**比 observer 那条轻**：它的 `_ws_handler` 首消息必须是 `auth` 且 token 有效，
    否则直接 `auth_error` + 关连接（`bridge.py:109-131`）。但**握手阶段照样白送一个连接**，
    而且 origin 校验在握手前就拦掉，比应用层判断更靠前。
    """
    import time
    from singularity.scheduler import bridge

    port = _free_port()
    bridge.start_ws_server(host="127.0.0.1", port=port)
    time.sleep(1.0)                      # 等线程起来（实测起服务在独立线程里）
    try:
        ok, detail = _run_ws_probe_on(port, "http://evil.example.com")
        assert not ok, f"外部 Origin 连上了 bridge 的 WS：{detail}"
    finally:
        bridge.stop_ws_server()
