"""观察者的**告警出口**与**活性痕迹** —— 2026-09-17 真机两个洞。

背景：观察者是**全仓唯一会主动发现"任务停滞"的角色**
（`witness.check_stalled()` 只有它和一个 admin 接口在用，调度器一次都不调），
而真机那晚一个任务卡了 **50 分钟、它一声没吭**。查下来：

  ① **一个字都不落盘** ⇒ "出声了但没人听"和"根本没出声"分不开
     （全量搜 `alerts.jsonl` = **0 条** observer）；
  ② 它唯一的出口 `_pending_replies` **几乎永远是空的** ——
     唯一正经的注册入口 `register_client` **全仓零调用者**，
     唯一写入点是聊天路径（有人问问题的那一刻才写）⇒ **广播给空字典、无声消失**。
     ⚠️ 而桥里**早就有能用的** `broadcast_observer`（`app.py` 的调度事件就在用它）。
"""
import json

import pytest

from singularity.scheduler import config
from singularity.scheduler import _observer_worker as W


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    return tmp_path


def test_落盘_异常要进_alerts_jsonl(_isolated):
    """① 落盘 —— 这是它**唯一的持久痕迹**。"""
    W._persist_alert({"kind": "stalled_task", "task_id": "t123", "message": "任务停滞超过 10 分钟"})
    line = (_isolated / "alerts.jsonl").read_text(encoding="utf-8").strip()
    assert line, "一个字都没落盘 ⇒ 事后查不出它到底出没出过声"
    rec = json.loads(line)
    assert rec["key"] == "observer_stalled_task", rec
    assert "t123" in rec["msg"], f"任务 id 要带上（不然追不了是哪个任务）: {rec}"


def test_广播_要走_bridge_那条真通道(monkeypatch):
    """② 走 `bridge.broadcast_observer` —— 而不是那个永远为空的 `_pending_replies`。"""
    from singularity.scheduler import bridge
    got = []
    monkeypatch.setattr(bridge, "broadcast_observer",
                        lambda event, data, **kw: got.append((event, data)) or 1)
    W._broadcast_via_bridge({"kind": "stalled_task", "message": "x"})
    assert got and got[0][0] == "observer_alert", \
        f"没走 bridge 的广播通道（那界面就收不到）: {got}"


def test_活性痕迹_写了能读回来(_isolated):
    W._write_state(loops=7, found_total=3, checks_last=1)
    st = W.read_state()
    assert st and st["loops"] == 7 and st["found_total"] == 3, st
    assert st["last_beat"] > 0


def test_活性痕迹_读不到要返回_None_不是空字典(_isolated):
    """**"没有"和"空"必须分得开** —— 返回 `{}` 会被上层读成"它在跑但什么都没记录"。"""
    assert W.read_state() is None


def test_接线_循环真调了这三条出口(monkeypatch, tmp_path):
    """🔴 **这条最要紧**：光有那三个函数不算，要证明**循环真的调了它们**。

    （"函数对" ≠ "接线通" —— 删掉 `_observer_worker` 里对应的那一行，这条必须红。）
    """
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(W, "_check_anomalies",
                        lambda: [{"kind": "stalled_task", "task_id": "t1", "message": "停滞"}])
    monkeypatch.setattr(W, "_persist_alert", lambda a: calls.append(("persist", a["kind"])))
    monkeypatch.setattr(W, "_broadcast_via_bridge", lambda a: calls.append(("bridge", a["kind"])))
    monkeypatch.setattr(W, "_write_state",
                        lambda **k: calls.append(("state", k["found_total"])))

    W._stop_event.clear()
    # 跑**一圈**就停：把 wait 换成立刻置停（否则要真等 5 秒）
    monkeypatch.setattr(W._stop_event, "wait", lambda _t: W._stop_event.set())
    W._observer_worker()

    assert ("persist", "stalled_task") in calls, f"循环里没调落盘: {calls}"
    assert ("bridge", "stalled_task") in calls, f"循环里没走 bridge 广播: {calls}"
    assert ("state", 1) in calls, f"循环里没写活性痕迹: {calls}"


def test_status_端点_没有痕迹时报_stale(monkeypatch, tmp_path):
    """`/api/observer/status` 要能看出"它聋了" —— 读不到痕迹就是 stale。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    from singularity.web.app import app
    c = app.test_client()
    r = c.get("/api/observer/status")
    assert r.status_code == 200, r.status_code
    body = r.get_json()
    assert body["stale"] is True and body["seconds_since_beat"] is None, body
    W._write_state(loops=1, found_total=0, checks_last=0)
    body = c.get("/api/observer/status").get_json()
    assert body["stale"] is False and body["seconds_since_beat"] is not None, body


def test_活性痕迹坏了要出声_不能跟没有混(_isolated, monkeypatch):
    """**"损坏和没有长得一样"** —— 本仓反复咬人的那个病。

    文件**不在** = "还没跑过"（正常，返回 None 就够）；
    文件**在但读不出来** = **坏了** ⇒ 必须出声，否则排障时看到的是"它没跑过"。
    """
    import singularity.scheduler.witness as _w
    warns = []
    monkeypatch.setattr(_w, "warn", lambda scope, msg, **kw: warns.append(kw.get("key")))

    (_isolated / "observer_state.json").write_text("{ 这不是 json", encoding="utf-8")
    assert W.read_state() is None
    assert "observer_state_unreadable" in warns, f"读坏了没出声: {warns}"
