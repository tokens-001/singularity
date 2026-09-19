"""观察者的异常告警要**讲人话**：事实 + 为什么 + 怎么办（2026-09-19 用户提）。

原话是「告警只报"出事了"，不报"为什么 + 怎么办"」。
`_check_anomalies()` 产出的每条告警原来只有 `{kind, message, ts}` —— **只有事实**，
看的人还得自己回去翻代码才知道该干嘛。§77.5 那轮修的是**出口**（落盘 + 广播），
这条修的是**内容**。

⚠️ 顺便钉住一个**文案 bug**：停滞告警原来硬编码"已停滞超过 **10 分钟**"，
而阈值是 `STALLED_AFTER_S = TASK_DEADLINE_S + 300 = 1200s`（**20 分钟**）——
**文案比实际少说了 10 分钟**。现在从常量算，改常量它自己会跟着变。
"""

import json

import pytest

from singularity.scheduler import _observer_client as C
from singularity.scheduler import _observer_worker as W
from singularity.scheduler import config, witness
from singularity.scheduler._observer_shared import _alert_history, _alert_lock


@pytest.fixture(autouse=True)
def _clean_history():
    """`_alert_history` 是模块级的去重表 —— 不清的话第二个用例什么都收不到。"""
    with _alert_lock:
        _alert_history.clear()
    yield
    with _alert_lock:
        _alert_history.clear()


def _only_stalled(monkeypatch, tid="t1"):
    monkeypatch.setattr(witness, "check_stalled", lambda *a, **k: [tid])
    return C._check_anomalies()


def _by_kind(alerts, kind):
    return [a for a in alerts if a.get("kind") == kind]


class TestStalledAlert:
    def test_带上了为什么和怎么办(self, monkeypatch):
        a = _by_kind(_only_stalled(monkeypatch), "stalled_task")[0]
        assert a.get("why"), "没有『为什么』—— 读的人还得回去翻代码"
        assert a.get("todo"), "没有『怎么办』—— 报警不给出路等于把活退回来"

    def test_怎么办里给了那一个任务的具体路径(self, monkeypatch):
        """别写成放之四海皆准的套话 —— 具体到这个 task 才有用。"""
        a = _by_kind(_only_stalled(monkeypatch, "t-abc"), "stalled_task")[0]
        assert "t-abc" in a["todo"] and "t-abc" in a["message"]

    def test_分钟数从常量算_不是手写的(self, monkeypatch):
        """🔴 原来硬编码"10 分钟"，而实际阈值是 1200s = 20 分钟 —— **文案在说谎**。"""
        a = _by_kind(_only_stalled(monkeypatch), "stalled_task")[0]
        expect = int(round(witness.STALLED_AFTER_S / 60))
        assert f"{expect} 分钟" in a["message"], (
            f"文案里的分钟数和 STALLED_AFTER_S({witness.STALLED_AFTER_S}s) 对不上：{a['message']}")
        assert "10 分钟" not in a["message"], "又写回那个错的硬编码了"

    def test_为什么不许断言机制以外的话(self, monkeypatch):
        """`why` 得说清"心跳陈旧 ≠ worker 死了"这层 —— 否则读的人会去重启/杀进程。"""
        a = _by_kind(_only_stalled(monkeypatch), "stalled_task")[0]
        assert "不是定时器" in a["why"], f"没点破心跳的粒度：{a['why']}"


class TestHeartbeatBacklogAlert:
    def test_带上了为什么和怎么办(self, monkeypatch, tmp_path):
        hb = config.QIDIAN_DIR / "heartbeats"
        hb.mkdir(parents=True, exist_ok=True)
        for i in range(201):
            (hb / f"t{i}_any.json").write_text("{}", encoding="utf-8")
        a = _by_kind(C._check_anomalies(), "heartbeat_backlog")[0]
        assert a.get("why") and a.get("todo"), a


class TestItReachesTheOnlyOutlet:
    """**"字段有了" ≠ "有人看得见"** —— 告警唯一真有人读的出口是 `alerts.jsonl`
    （告警页按 key 聚合读的就是它）。这条钉"落盘那句真的把它们带出去了"。"""

    def test_落盘文本里带上为什么和怎么办(self, monkeypatch):
        alerts = []
        monkeypatch.setattr(W.witness, "warn",
                            lambda scope, msg, **kw: alerts.append(msg))
        W._persist_alert({"kind": "stalled_task", "task_id": "t1",
                          "message": "任务 t1 停滞", "why": "因为甲", "todo": "所以就做乙"})
        assert len(alerts) == 1
        assert "为什么：因为甲" in alerts[0], alerts[0]
        assert "怎么办：所以就做乙" in alerts[0], alerts[0]

    def test_没有_why_todo_时不能崩_也不能印出_None(self, monkeypatch):
        """边界：老代码 / 别处塞进来的 alert 可能只有事实。"""
        alerts = []
        monkeypatch.setattr(W.witness, "warn",
                            lambda scope, msg, **kw: alerts.append(msg))
        W._persist_alert({"kind": "x", "message": "只有事实"})
        assert "None" not in alerts[0], alerts[0]
        assert "只有事实" in alerts[0]

    def test_端到端_真的写进了_alerts_jsonl(self, monkeypatch):
        import pathlib
        p = pathlib.Path(config.QIDIAN_DIR) / "alerts.jsonl"
        W._persist_alert({"kind": "stalled_task", "task_id": "t9",
                          "message": "任务 t9 停滞", "why": "因为甲", "todo": "做乙"})
        recs = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert any("为什么：因为甲" in r.get("msg", "") for r in recs), recs
