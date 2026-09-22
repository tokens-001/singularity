"""「只想不写」那把尺（`_OUTPUT_IDLE_LIMIT`）—— 2026-09-22 用户拍板 480s + 先劝一轮。

**来历**：`round-20260922` 实测 —— 去掉 240s 硬顶（`60a81e16`）之后，打转的调用
不再 240s 止损，而是**烧到剩余预算见底**（实测 cap 809~945s，代价 3.4~3.8 倍）。
而原来那把停滞尺（`_STALL_TIMEOUT`，90s）**认 `reasoning_content` 是进展**
（那是对的，"正在想"不是"卡住"），代价是"想了几万字、正文一个字没有"那种形状
它**永远掐不到**。所以需要第二把尺：**量多久没吐出正文/工具调用，思考不算**。

**阈值的取数**（`docs/轮次成绩单-20260922.md` §三，单轮 33 条分诊账）：
  · 未被掐的 25 条 `elapsed` 落在 20~222.8 秒
  · 打转被掐的 8 条落在 904~945 秒
⇒ 两组**完全不相交**；480 是前者的 2.15 倍余量。
⚠️ **圈数 `loops` 不能当判据**（实测重叠：正常 269~7451 vs 打转 448~3734）。

🔴 **接线比函数本身更容易断**：`_NoOutputError` 是 `_NetworkError` 的**子类**，
而 `_api_call` 对 `_NetworkError` 是**原样重试**的 ⇒ 漏掉 `_api_call` 里那条
`except _NoOutputError: raise`，"只想不写"会被静默重试掉，turn 循环里那支
"先劝一轮"**永远轮不到**（函数对 ≠ 接线通）。下面有用例专门钉这一条。
"""
import http.server
import threading
import time

import httpx
import pytest

from singularity.scheduler.executors import openai_agent as oa


def _executor(tid="t_output_idle"):
    from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor
    ex = OpenAIAgentExecutor({"model": "m", "api_key_env": "K"}, "测试任务", tid, cwd=".")
    ex._api_key = "k"
    ex._url = "http://x"
    ex._is_responses_api = False
    return ex


# ══════════════════════════════════════════════════════════════
# 一、走路：它必须仍是 `_NetworkError` 子类
# ══════════════════════════════════════════════════════════════

def test_只想不写仍是网络错误的子类():
    """捕获点写的是 `except (_NetworkError, _FormatError)` —— 改成平级的新异常，
    那几处就接不住、异常会直接穿出去（行为变了）。同 `_StalledError` 的理由。"""
    assert issubclass(oa._NoOutputError, oa._NetworkError)


# ══════════════════════════════════════════════════════════════
# 二、接线：`_api_call` **不许重试**它
# ══════════════════════════════════════════════════════════════

def test_api_call_不重试只想不写(monkeypatch):
    """🔴 **这条钉的是接线，不是函数**。

    `_NoOutputError` 是 `_NetworkError` 的子类，而 `_api_call` 那条
    `except (_NetworkError, _TransientError)` 会**原样重试** ⇒ 漏掉
    `except _NoOutputError: raise`，同一个模型拿同一个 prompt 会再打转一个
    `_OUTPUT_IDLE_LIMIT`，而 turn 循环那支"先劝一轮"**永远轮不到**。

    变异验证：删掉 `_api_call` 里那句 `except _NoOutputError: raise` → 红
    （`_api_call_once` 会被调多次，且中间会 sleep）。
    """
    ex = _executor()
    seen = []

    def _once(body):
        seen.append(1)
        raise oa._NoOutputError("只想不写 480s：一直没有正文/工具调用")

    monkeypatch.setattr(ex, "_api_call_once", _once)

    with pytest.raises(oa._NoOutputError):
        ex._api_call({"model": "m", "messages": []})

    assert len(seen) == 1, (
        f"`_NoOutputError` 被重试了 {len(seen)} 次 —— 原样重试 = 再打转一个 480 秒；"
        f"且 turn 循环里那支'先劝一轮'永远轮不到")


# ══════════════════════════════════════════════════════════════
# 三、尺子真的会触发（用**真 HTTP 服务器** —— 替身喂的是我猜的行为）
# ══════════════════════════════════════════════════════════════

class _OnlyReasoningServer:
    """一直在发**合法 SSE 行**，但 `delta` 里**只有 reasoning_content** ——
    正是"想了几万字、正文一个字没有"的那个形状。"""

    def __init__(self, feed_s: float, with_content: bool = False):
        self.feed_s = feed_s
        self.with_content = with_content

    def __enter__(self):
        feed_s, with_content = self.feed_s, self.with_content

        class _H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                t_end = time.time() + feed_s
                i = 0
                try:
                    while time.time() < t_end:
                        if with_content:
                            body = 'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'.encode()
                        else:
                            body = 'data: {"choices":[{"delta":{"reasoning_content":"想"}}]}\n\n'.encode()
                        self.wfile.write(body)
                        self.wfile.flush()
                        i += 1
                        time.sleep(0.05)
                except Exception:
                    pass

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self.srv

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()
        return False


class TestRuler:
    def _run(self, monkeypatch, srv, limit):
        ex = _executor()
        ex._url = f"http://127.0.0.1:{srv.server_address[1]}/chat/completions"
        ex._deadline_at = time.time() + 60.0     # 别让"剩余预算"先到，要让这把尺先触发
        # 停滞那把尺调大：这一条要测的是**第二把**尺，别让它抢答
        monkeypatch.setattr(oa, "_STALL_TIMEOUT", 30.0)
        monkeypatch.setattr(oa, "_OUTPUT_IDLE_LIMIT", limit)
        monkeypatch.setattr(oa, "_get_http_client", lambda: httpx.Client())
        t0 = time.time()
        try:
            with pytest.raises(oa._NoOutputError) as ei:
                ex._stream_call({"model": "m", "messages": []})
            return ei.value, time.time() - t0
        finally:
            pass

    def test_只想不写会被掐断(self, monkeypatch):
        """只吐 reasoning ⇒ 到 `_OUTPUT_IDLE_LIMIT` 就断，**不等服务器收工**。

        变异验证：删掉 `_lines()` 里那段 `_out_idle > _OUTPUT_IDLE_LIMIT` → 红
        （会一直等到服务器收工、然后正常返回一个空回答）。
        """
        with _OnlyReasoningServer(feed_s=8.0) as srv:
            err, elapsed = self._run(monkeypatch, srv, limit=1.0)
        assert elapsed < 4.0, f"{elapsed:.1f}s 才断 —— 服务器要吐 8 秒，说明没掐住"
        assert "只想不写" in str(err)

    def test_吐了正文就不掐(self, monkeypatch):
        """**反例**：正文一直在长 ⇒ 这把尺**不该**触发。

        它守的是"阈值定低了会误杀'想很久、然后一次吐完'的调用"那一侧 ——
        这条用例只证明"有产出就不掐"，**不能**证明 480 秒这个数选对了
        （那要靠 `docs/轮次成绩单-20260922.md` §三 的两组实测，不是靠这个测试）。
        """
        with _OnlyReasoningServer(feed_s=3.0, with_content=True) as srv:
            ex = _executor()
            ex._url = f"http://127.0.0.1:{srv.server_address[1]}/chat/completions"
            ex._deadline_at = time.time() + 60.0
            monkeypatch.setattr(oa, "_STALL_TIMEOUT", 30.0)
            monkeypatch.setattr(oa, "_OUTPUT_IDLE_LIMIT", 1.0)
            monkeypatch.setattr(oa, "_get_http_client", lambda: httpx.Client())
            try:
                ex._stream_call({"model": "m", "messages": []})
            except oa._NoOutputError as e:            # pragma: no cover
                pytest.fail(f"正文一直在长却被「只想不写」掐了：{e}")
            except Exception:
                pass                                   # 别的尺子/收尾异常与本条无关


# ══════════════════════════════════════════════════════════════
# 四、掐了之后：**先劝一轮，第二次才判失败**
# ══════════════════════════════════════════════════════════════

def test_先劝一轮_第二次才判失败(monkeypatch):
    """2026-09-22 用户拍板：「先注入『别想了、现在写文件』再给一轮」。

    判据三条：
      · 第一次掐断后**没有**直接失败，而是又调了一次（`len(bodies) == 2`）
      · 第二次那次请求里**带上了那句系统指令**（否则"劝"根本没发生）
      · 第二次还掐 ⇒ 判失败，**不再劝第三轮**（`_no_output_urged` 是局部变量）

    变异验证：
      · 去掉 `messages.append(...)` 那段 → 第二条断言红
      · 去掉 `_no_output_urged` 判断 → `len(bodies)` 变成 `max_turns`（不再等于 2）
    """
    ex = _executor()
    bodies = []

    def _boom(body):
        bodies.append(body)
        raise oa._NoOutputError("只想不写 480s：一直没有正文/工具调用")

    monkeypatch.setattr(ex, "_api_call", _boom)
    res = ex.run()

    assert len(bodies) == 2, f"劝了/调了 {len(bodies)} 次，期望 2 次（第一次掐 + 劝后一次）"
    urge = [m for m in bodies[1].get("messages", [])
            if str(m.get("content", "")).startswith("[系统]")]
    assert any("停止思考" in str(m.get("content")) for m in urge), \
        f"第二次请求里没有那句'别想了、现在写文件'：{[m.get('content') for m in urge]}"
    assert not res.success, "劝了两轮还不产出，应当判失败"
