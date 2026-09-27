"""别名对账：**流式那条路上也得记**（2026-09-27）。

**来历**：`.qidian/api_store.json` 里**压根没有 `_aliases` 这个键** —— 一条都没记过。
根因不是逻辑错，是**闸门装在一条默认不走的路上**：
`record_alias` 只写在 `openai_agent._api_call_once` 的**非流式**分支
（`_STREAM = os.environ.get("QIDIAN_STREAM","1") != "0"` ⇒ **默认走流式**），
而流式那条（`_stream_call`）**一次都没调过它**。

**代价分两半**：
  ① **能力**：`dispatcher.pick_agent_fallback_chain` 那段别名去重恒空转 ——
     而 `phase_models.json` 的 planning 席位是
     `["deepseek-flash","glm-5.3-flash","deepseek-v4-pro"]`，
     **第 1 和第 3 实际是同一个模型** ⇒ "多视角碰撞"是**自己跟自己碰**，
     而那段注释说这是"唯一验证过有价值的那个能力"。
  ② **钱**：计价按请求名走（`deepseek-v4-pro` 1.848 / `deepseek-flash` 0.48）⇒ 高估 3.85 倍。
     ⚠️ **这一半本次没动** —— 它取决于"实际按哪个价计费"，而那个数清单上还挂着
     （待用户给账单实际扣费数）。**别拿推断去改钱的路。**

🔵 这是 `OPEN.md` 那条老形状的**第三次**：「闸门只加在其中一份实现上，而那份没人走」。
"""
import http.server
import json
import threading

import httpx
import pytest

from singularity.scheduler import api_store
from singularity.scheduler.executors import openai_agent as oa


def _executor(requested="deepseek-v4-pro", tid="t_alias"):
    from singularity.scheduler.executors.openai_agent import OpenAIAgentExecutor
    ex = OpenAIAgentExecutor({"model": requested, "api_key_env": "K"}, "测试任务", tid, cwd=".")
    ex._api_key = "k"
    ex._is_responses_api = False
    return ex


class _SSEServer:
    """真·本地 SSE 服务器 —— 每个 chunk 带一个 `model` 字段（像厂商那样）。"""

    def __init__(self, actual_model: str):
        self.actual = actual_model

    def __enter__(self):
        actual = self.actual

        class _H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for piece in ('{"choices":[{"delta":{"content":"好"}}],"model":"%s"}\n\n' % actual,
                              '{"choices":[{"delta":{"content":"的"}}],"model":"%s"}\n\n' % actual,
                              "[DONE]\n\n"):
                    self.wfile.write(("data: " + piece).encode())
                    self.wfile.flush()
                try:
                    self.wfile.flush()
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


def _aliases_on_disk() -> dict:
    p = api_store._store_path()
    if not p.exists():
        return {}
    return (json.loads(p.read_text(encoding="utf-8")) or {}).get("_aliases") or {}


# ══════════════════════════════════════════════════════════════
# 一、流式这条路真的会记（这是本次的修复点）
# ══════════════════════════════════════════════════════════════

def test_流式调用会记下别名(monkeypatch):
    """🔴 **变异**：删掉 `_stream_call` chunk 循环里那句 `record_alias` ⇒ 本条红。

    这条**必须是真流式**：默认 `_STREAM=1`，而原来的对账只在非流式那支 ——
    单独测非流式的话，测的是一条**没人走的路**（正是这次出事的形状）。
    """
    ex = _executor(requested="deepseek-v4-pro")
    with _SSEServer("deepseek-v4-flash") as srv:
        ex._url = f"http://127.0.0.1:{srv.server_address[1]}/chat/completions"
        ex._deadline_at = __import__("time").time() + 30.0
        monkeypatch.setattr(oa, "_get_http_client", lambda: httpx.Client())
        out = ex._stream_call({"model": "deepseek-v4-pro", "messages": []})

    assert out["choices"][0]["message"]["content"] == "好的", out
    assert _aliases_on_disk().get("deepseek-v4-pro") == "deepseek-v4-flash", \
        "流式那条路没记别名 —— `_aliases` 还是空的，委员会去重照样空转"


def test_名字一样就不写盘(monkeypatch):
    """**反方向对照**：请求名 == 返回名 ⇒ 不该写任何映射。

    否则每个模型都往 `_aliases` 里塞一条恒等映射，"谁是别名"这件事就被淹了。
    """
    ex = _executor(requested="deepseek-flash")
    with _SSEServer("deepseek-flash") as srv:
        ex._url = f"http://127.0.0.1:{srv.server_address[1]}/chat/completions"
        ex._deadline_at = __import__("time").time() + 30.0
        monkeypatch.setattr(oa, "_get_http_client", lambda: httpx.Client())
        ex._stream_call({"model": "deepseek-flash", "messages": []})
    assert _aliases_on_disk() == {}, _aliases_on_disk()


# ══════════════════════════════════════════════════════════════
# 二、记下来之后，**用它的那一跳**真的变了
# ══════════════════════════════════════════════════════════════

def test_canonical_跟着链走():
    """A→B→C 要走到头 —— 厂商连换两次名的话，只跳一跳就不够了。"""
    api_store.record_alias("m-old", "m-mid")
    api_store.record_alias("m-mid", "m-new")
    assert api_store.canonical("m-old") == "m-new"
    assert api_store.canonical("m-untouched") == "m-untouched"


# ⚠️ **"记下来之后去重真的生效"那一条不在这里** ——
#    `test_silent_failure_invariants.py` 的 `TestAliasDedup` 已经钉过了
#    （`record_alias("stale-alias","real-model")` ⇒ 链里只剩 `["real-model","other"]`）。
#    本次坏的是**上游**（流式那条路压根没记），不是下游那一跳 —— 所以这里只补上游，
#    **不复制一份下游的测试**（同一件事写两遍就是两份会漂的判据）。
