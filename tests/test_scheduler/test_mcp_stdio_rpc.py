"""MCP stdio 的请求/响应必须**按 id 认领** —— 真起一个子进程说话。

出处：外派 扫bug-02 的 ③（我回当前树核过）。原来 `_recv_stdio` 是
「`readline` 一行 → `json.loads` → 交差」，**从不看 `id`**。两个后果：

  · 服务端发**通知**（`notifications/*`，按协议就是没有 `id` 的）或往 stdout 打日志时，
    返回的是那一条 —— **不是这次请求的响应**；
  · 更坏的是**超时之后**：30s 没等到就 `return None`，而响应还躺在管道里
    ⇒ **下一次调用读到的是上一次的响应**，从此整体串位，而且**全程不报错**。

这两条都**只测行为**：拉一个真的 Python 子进程当假 MCP 服务器，按剧本说话。
不测源码里有没有 `want_id` 这种形状 —— 那测的是"我改没改"，不是"门拦没拦住"。
"""
import json
import subprocess
import sys
import textwrap
import time

import pytest

from singularity.scheduler.mcp import MCPClient, MCPServerConfig


def _client_with_script(tmp_path, body: str) -> MCPClient:
    """起一个假 MCP 服务器（真子进程），把它的 stdin/stdout 接到一个 MCPClient 上。"""
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")

    client = MCPClient(MCPServerConfig(
        name="fake", transport="stdio",
        command=f"{sys.executable} -u {script}"))
    client._proc = subprocess.Popen(
        [sys.executable, "-u", str(script)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True)
    return client


def _kill(client: MCPClient) -> None:
    try:
        client._proc.kill()
        client._proc.wait(timeout=5)
    except Exception:
        pass


# ── ① 通知 / 日志行不许被当成响应 ────────────────────────────────

def test_通知和日志行不许被当成响应(tmp_path):
    """服务端先发通知、再打一行日志、**最后**才发真响应。

    看 id 之前，`_recv_stdio` 会拿到那条**通知**就当响应交回去 ——
    它是合法 JSON、形状也像，只是**没有 `result`**，
    于是上层看到的是"工具调用返回了看不懂的东西"，而不是一个错误。
    """
    client = _client_with_script(tmp_path, '''
        import sys, json
        for line in sys.stdin:
            req = json.loads(line)
            # ① 协议里的通知：按规范就没有 id
            sys.stdout.write(json.dumps({"jsonrpc": "2.0",
                "method": "notifications/message", "params": {}}) + "\\n")
            # ② 有些服务器会往 stdout 打日志
            sys.stdout.write("server: warming up\\n")
            sys.stdout.flush()
            # ③ 最后才是这次请求真正的响应
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"],
                "result": {"echo": req["method"]}}) + "\\n")
            sys.stdout.flush()
    ''')
    try:
        got = client._rpc_stdio("tools/list", {})
    finally:
        _kill(client)

    assert got is not None, "整条丢了"
    assert got.get("result") == {"echo": "tools/list"}, \
        f"拿回来的不是这次请求的响应（很可能读到了通知/日志行）：{got}"


# ── ② 超时之后，迟到的响应不许喂给下一次调用 ─────────────────────

def test_迟到响应串位_显式超时版(tmp_path):
    """同上，但用 `_recv_stdio` 显式给短超时 —— 不依赖默认的 30s，测试跑得快。

    走的是同一条路（`_rpc_stdio` 里的 `with self._stdio_lock` + `_recv_stdio(req_id)`），
    只是超时参数由测试控制。
    """
    client = _client_with_script(tmp_path, '''
        import sys, json, time
        first = True
        for line in sys.stdin:
            req = json.loads(line)
            if first:
                first = False
                time.sleep(1.2)
                which = "第一次的响应（迟到了）"
            else:
                which = "第二次的响应"
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"],
                "result": {"which": which}}) + "\\n")
            sys.stdout.flush()
    ''')
    try:
        # 第 1 次：0.3s 超时 ⇒ 一定拿不到（服务端睡 1.2s）
        client._send_stdio(json.dumps(
            {"jsonrpc": "2.0", "id": 101, "method": "tools/call", "params": {}}) + "\n")
        assert client._recv_stdio(101, timeout=0.3) is None, "不该拿到（服务端还没说话）"

        # 等到那条迟到的响应进管道
        time.sleep(1.3)

        # 第 2 次：必须拿到**自己的**（id=202），而不是管道里那条 id=101 的
        client._send_stdio(json.dumps(
            {"jsonrpc": "2.0", "id": 202, "method": "tools/call", "params": {}}) + "\n")
        got = client._recv_stdio(202, timeout=5.0)
    finally:
        _kill(client)

    assert got is not None, "整条丢了"
    assert got.get("id") == 202, f"拿回来的是别的请求的响应：{got}"
    assert got["result"]["which"] == "第二次的响应", \
        f"串位了 —— 第 2 次调用读到了第 1 次的迟到响应：{got}"


# ── ③ 并发的两个往返不许互相抢 ──────────────────────────────────

def test_并发往返不许互相抢(tmp_path):
    """stdio 只有一条管道。两个线程同时往返时，光按 id 判还不够 ——
    各自的"不是我的"会被对方丢掉，两边一起超时。所以发+收要在一把锁里。"""
    import threading
    client = _client_with_script(tmp_path, '''
        import sys, json
        for line in sys.stdin:
            req = json.loads(line)
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"],
                "result": {"echo": req["method"]}}) + "\\n")
            sys.stdout.flush()
    ''')
    results, errs = {}, []

    def call(i):
        try:
            r = client._rpc_stdio(f"method-{i}", {})
            results[i] = (r or {}).get("result", {}).get("echo")
        except Exception as e:                  # noqa: BLE001
            errs.append(repr(e))

    try:
        ts = [threading.Thread(target=call, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=20)
    finally:
        _kill(client)

    assert not errs, f"并发往返炸了：{errs[:3]}"
    # 每个线程都必须拿到**自己那次**的 echo —— 混一个就是串位
    for i in range(6):
        assert results.get(i) == f"method-{i}", \
            f"线程 {i} 拿到了别人的结果：{results}"
