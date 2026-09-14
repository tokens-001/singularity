"""两个 WS 服务的**逐连接鉴权**（2026-09-14）。

起因：`QIDIAN_AUTH=1` 时 HTTP 那侧全员要 token，而**两个 WS 服务全文没有一个 token 判定**
—— "配置说要鉴权"这件事在 WS 上不成立。而这两个服务认识的 action 里有
`chat` / `create_task` / `delete_task` / `control_loop`。

挡住浏览器的那道仍是 **Origin 校验**（`ws_allowed_origins`，另一条测试钉着）；
这条补的是**同一套开关要管住所有的门**，默认关 ⇒ 行为逐字不变。
"""


class _Req:
    def __init__(self, path="/", headers=None):
        self.path = path
        self.headers = headers or {}


class _Conn:
    def __init__(self):
        self.rejected = []

    def respond(self, status, text):
        self.rejected.append((status, text))
        return ("REJECT", status)


def test_默认不要求_token_行为不变(monkeypatch):
    """`QIDIAN_AUTH` 没设（默认）⇒ 放行 —— 别把默认路径改成要鉴权。"""
    from singularity.scheduler import _auth
    monkeypatch.delenv("QIDIAN_AUTH", raising=False)
    c = _Conn()
    assert _auth.ws_authorize(c, _Req("/")) is None, "默认配置下不该拦"
    assert c.rejected == []


def test_开了鉴权_无token或错token都拒(monkeypatch):
    from singularity.scheduler import _auth
    monkeypatch.setenv("QIDIAN_AUTH", "1")
    for path, headers in (("/", {}), ("/?token=假的", {}), ("/", {"Authorization": "Bearer 假"}) ):
        c = _Conn()
        r = _auth.ws_authorize(c, _Req(path, headers))
        assert r is not None and c.rejected and c.rejected[0][0] == 401, (path, headers, r)
        # 拒绝原因要说清是**鉴权没过**，不是"服务端坏了"
        assert "token" in c.rejected[0][1]


def test_开了鉴权_两种带token的方式都收(monkeypatch):
    """query 和 `Authorization: Bearer` 都要收：**浏览器不能给 `new WebSocket()` 设自定义头**
    （只能挂 query），而脚本用头更自然 —— 这条路的客户端两种都有。"""
    from singularity.scheduler import _auth
    monkeypatch.setenv("QIDIAN_AUTH", "1")
    monkeypatch.setattr(_auth, "get_auth", lambda: type(
        "S", (), {"authenticate": staticmethod(lambda t: t == "好token")})())
    assert _auth.ws_authorize(_Conn(), _Req("/?token=好token")) is None
    assert _auth.ws_authorize(_Conn(), _Req("/", {"Authorization": "Bearer 好token"})) is None
    assert _auth.ws_authorize(_Conn(), _Req("/?token=坏")) is not None


def test_鉴权自己坏了是拒绝不是放行(monkeypatch):
    """`get_auth()` 抛 ⇒ **不许 catch 成放行**。
    websockets 对 `process_request` 抛异常的处理是**拒绝握手（500）**
    （`asyncio/server.py:148-157`）⇒ 抛 = 拒，这是 fail-closed 的那一侧。
    变异：在 `ws_authorize` 外面包一层 `except: return None` → 本用例红。"""
    import pytest
    from singularity.scheduler import _auth
    monkeypatch.setenv("QIDIAN_AUTH", "1")

    def boom():
        raise RuntimeError("用户库坏了")
    monkeypatch.setattr(_auth, "get_auth", boom)
    with pytest.raises(RuntimeError):
        _auth.ws_authorize(_Conn(), _Req("/?token=x"))


def test_HTTP那侧的开关和WS是同一个(monkeypatch):
    """`web/app.py` 的 `_AUTH_ENABLED` 必须**来自** `_auth.auth_enabled`，
    不许各读一遍环境变量（同一件事写两处，改一处漏一处）。"""
    from singularity.scheduler import _auth
    monkeypatch.setenv("QIDIAN_AUTH", "1")
    assert _auth.auth_enabled() is True
    monkeypatch.setenv("QIDIAN_AUTH", "0")
    assert _auth.auth_enabled() is False
    monkeypatch.delenv("QIDIAN_AUTH", raising=False)
    assert _auth.auth_enabled() is False


def test_observer_把鉴权接上了(monkeypatch):
    """**接线**：上面那些测的是函数本体 —— 接通没接通是另一回事。
    这里把 `serve` 换成捕获参数的桩，看 observer 那个入口传没传 `process_request`。

    ⚠️ **只测 observer**：`bridge`（5051）**本来就有**连接级鉴权
    （`_ws_handler` 要求首条消息是 `auth`），我一开始按"两个 WS 零鉴权"的
    说法给它也加了一层，**已撤**（那会让按它自己协议认证过的客户端在握手时被拒）。

    ⚠️ **必须打各自模块里的名字**：observer 是 `from websockets.asyncio.server import serve`
    （模块级绑定了），只打 `websockets.serve` 拦不住它 —— 第一版就是这么**真去 bind 了
    8765**，而本机**正跑着一个奇点实例**，直接 `address already in use`。
    变异：把 `process_request=ws_authorize` 从任一入口删掉 → 红。
    """
    import asyncio
    import websockets
    from singularity.observer import server as obs
    from singularity.scheduler import bridge

    seen = []

    class _FakeServer:
        """observer 用的是 `await serve(...)` 的返回值（不是 async with），
        所以这个桩**要能 await**，同时也要能当上下文管理器（bridge 用 async with）。"""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __await__(self):
            async def _self():
                return self
            return _self().__await__()

        def close(self):
            return None

        async def wait_closed(self):
            return None

    def fake_serve(handler, host, port, **kw):
        seen.append(kw)
        return _FakeServer()

    monkeypatch.setattr(websockets, "serve", fake_serve)   # bridge 走 websockets.serve
    monkeypatch.setattr(obs, "serve", fake_serve)          # observer 走模块级的 serve

    srv = obs.ObserverServer()
    loop = asyncio.new_event_loop()
    try:
        async def _go():
            await srv.start()
            srv._heartbeat_task.cancel()
        loop.run_until_complete(_go())
    finally:
        loop.close()

    assert len(seen) == 1, f"没抓到 observer 的 serve 调用：{seen}"
    from singularity.scheduler._auth import ws_authorize
    kw = seen[0]
    assert kw.get("process_request") is ws_authorize, f"observer 没接鉴权：{sorted(kw)}"
    assert "origins" in kw, "Origin 那道门被顺手删了？"


def test_bridge_的连接级鉴权还在(monkeypatch):
    """`bridge`（5051）**自己那套**：首条消息必须是 `auth`，token 不对就关连接。
    这条是**既有行为**（不是今天加的），钉住它是为了记住"别给它再加一层"。"""
    import asyncio
    import json
    from singularity.scheduler import bridge

    sent = []

    class _WS:
        """⚠️ 桩要能接 `bridge._WSClient.send_json` —— 它内部是
        `await self.ws.send(json.dumps(...))`（**不是** `send_json`），
        第一版我桩错了方法名，于是"没收到消息"和"这段根本没跑"分不开。"""

        async def recv(self):
            return '{"method":"auth","params":{"token":"假的"}}'

        async def send(self, raw):
            sent.append(json.loads(raw))

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    monkeypatch.setattr(bridge, "add_client", lambda c: None)
    monkeypatch.setattr(bridge, "remove_client", lambda c: None)
    asyncio.run(bridge._ws_handler(_WS()))
    assert sent and sent[0]["method"] == "auth_error", sent
