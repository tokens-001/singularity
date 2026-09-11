"""融合那几步的用量要记到**项目账**上，不能一律进 `_unknown` 桶。

2026-09-11 探路轮实测：融合子步骤（提取/辩论/定稿）记了 86,344 tokens 进 `_unknown`，
一条没进项目账 —— 而它是整条流水线**单次最贵**的调用（两席初稿才 35,690，
融合是它的 2.4 倍）。项目详情页因此看不到这笔钱。

链路：`_safe_dispatch(project_id)` → `dispatch` → `_dispatch_committee`
→ `fuse_architecture_v2` → `_cm` → `_call_model` → `_stream_once`（真正记账那层）。
"""
from singularity.scheduler import execution_judge as ej


class _Resp:
    status_code = 200

    def iter_lines(self):
        yield ('data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}],'
               '"usage":{"total_tokens":42}}')
        yield "data: [DONE]"


class _Ctx:
    def __enter__(self):
        return _Resp()

    def __exit__(self, *a):
        return False


class _Client:
    def stream(self, *a, **kw):
        return _Ctx()


def test_stream_records_under_project_when_given(monkeypatch):
    """给了 project_id → 走 record_tokens（能算钱、进项目账）。"""
    from singularity.scheduler import _token_budget as tb
    rows = []
    monkeypatch.setattr(tb, "record_tokens", lambda **kw: rows.append(kw))
    monkeypatch.setattr(tb, "record_system_tokens",
                        lambda **kw: rows.append({"SYSTEM": True, **kw}))

    ej._stream_once(_Client(), "http://x", {}, {"model": "m-1"}, project_id="proj-9")

    assert len(rows) == 1, rows
    assert rows[0].get("project_id") == "proj-9", "没带 project_id → 又进 _unknown 桶"
    assert rows[0]["tokens"] == 42
    assert rows[0]["model"] == "m-1"
    assert rows[0]["level"] == "fusion"


def test_stream_falls_back_to_system_tokens(monkeypatch):
    """没给 project_id（系统级调用）→ 保持老行为，记进 _unknown。"""
    from singularity.scheduler import _token_budget as tb
    rows = []
    monkeypatch.setattr(tb, "record_tokens", lambda **kw: rows.append({"PROJ": True, **kw}))
    monkeypatch.setattr(tb, "record_system_tokens",
                        lambda **kw: rows.append({"SYSTEM": True, **kw}))

    ej._stream_once(_Client(), "http://x", {}, {"model": "m-1"})

    assert len(rows) == 1 and rows[0].get("SYSTEM"), "系统级调用不该被强行归到某个项目"


def test_call_model_forwards_project_id(monkeypatch):
    """`_call_model` 必须把 project_id 透传给 `_stream_once`（链路不能中途断）。"""
    seen = {}
    monkeypatch.setattr(ej, "_resolve_api", lambda m: ("FAKE_KEY_X", "http://x"))
    monkeypatch.setenv("FAKE_KEY_X", "k")

    def fake_stream(client, base_url, headers, payload, project_id=""):
        seen["project_id"] = project_id
        return 200, "hi", "stop", "", ""

    monkeypatch.setattr(ej, "_stream_once", fake_stream)
    assert ej._call_model("p", "m", project_id="proj-7") == "hi"
    assert seen["project_id"] == "proj-7"


def test_fuse_signature_accepts_project_id():
    """`fuse_architecture_v2` 得收得下 project_id（关键字，不破坏老调用方）。"""
    import inspect
    sig = inspect.signature(ej.fuse_architecture_v2)
    assert "project_id" in sig.parameters
    assert sig.parameters["project_id"].default == ""
