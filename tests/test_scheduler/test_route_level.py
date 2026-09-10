"""`route.level` 从来不存在 —— 4 处代码读它，每任务必抛 AttributeError。

两档制后 level 已废弃：`pre_search.apply_escalation` 的注释写明「两档后不再自动升级,
只标记信号供 trace」，`tracker.Task.route_level` 恒为 "any"。所以 `RouteResult` 只有
task_type / gate_required / matched_signals / cached_at 是**对的**，错的是还在读
`route.level` 的四处调用点。

实测代价（2026-09-10 一条真流水线跑出来，7 个任务 7 次告警）：
  · _task_runner 那处在 try 块**开头**，异常把同一个 try 里的
    record_tokens 和 learner.record 一并跳过 → 用量统计 / 路由学习 / 经验归档三个子系统全静默失效
  · _exec 那处让 mem.update_attrs 整段没执行（记忆里的终态和 route 属性从没更新过）
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "singularity"


def test_route_result_has_no_level():
    """前提：RouteResult 没有 level。哪天有人加回来了，本文件的其余断言要重审。"""
    from singularity.scheduler import router
    assert not hasattr(router.RouteResult(), "level")


def test_no_source_reads_route_level():
    """全仓扫一遍，防止修复只覆盖了已知的 4 处。"""
    bad = []
    for p in SRC.rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]          # 注释里可以提 route.level，代码里不行
            if re.search(r"\broute\.level\b", code):
                bad.append(f"  {p.relative_to(SRC)}:{i}  {line.strip()}")
    assert not bad, "route 没有 level 字段，应该读 task.route_level：\n" + "\n".join(bad)


def test_save_trace_records_route_level(tmp_path, monkeypatch):
    """端到端：旧代码在这条路上抛 AttributeError，update_attrs 收不到任何调用。"""
    from singularity.scheduler import _exec, config, router, tracker

    (tmp_path / "traces").mkdir()
    monkeypatch.setattr(config, "TRACE_DIR", tmp_path / "traces")

    seen: dict = {}
    monkeypatch.setattr(_exec.nj_mod, "build_report", lambda **kw: {})
    monkeypatch.setattr(_exec.nj_mod, "save_trace", lambda *a, **k: None)
    monkeypatch.setattr(_exec.mem_mod, "index_task", lambda **kw: None)
    monkeypatch.setattr(_exec.mem_mod, "update_attrs",
                        lambda tid, **kw: seen.update(kw, _tid=tid))

    task = tracker.Task(id="t-route-level", description="x")
    _exec._save_trace(task, router.RouteResult(task_type="feature"), None, None, None, False)

    assert seen, "update_attrs 没被调用 —— 异常又被 except 吞了"
    assert seen["route_level"] == "any"          # 两档制后恒为 any
    assert seen["route_type"] == "feature"       # route.task_type 是存在的字段
    assert seen["status"] == "pending"


def test_experience_record_roundtrip():
    """ExperienceRecord 必须能"构造 → 序列化 → 读回"走通。

    它的 to_dict / from_dict / archive_experience 三处都用 route_level，
    而 dataclass 的字段声明漏了它 —— 构造抛 TypeError、序列化抛 AttributeError，
    两头都炸 ⇒ **经验归档从上线起一次都没成功过**（2026-09-11 真机验证抓到：
    跑完一个任务，experiences.json 根本没被创建）。
    """
    from singularity.scheduler._memory_experience import ExperienceRecord

    rec = ExperienceRecord(task_id="t1", description="d", status="done",
                           route_level="any", model="m", elapsed_ms=1.0)
    assert rec.to_dict()["route_level"] == "any"
    assert ExperienceRecord.from_dict(rec.to_dict()).route_level == "any"
