"""守卫：测试不许写生产 `.qidian/`。

被测代码里 witness.warn / tracker 落盘都是真写文件的。没有 tests/conftest.py 那个
_isolate_qidian_dir 夹具时，整套测试会往生产 `.qidian/alerts.jsonl` 追加十几行 ——
而那个文件正是拿来查故障的，混进测试噪声就查不出真东西了。

这条测试在夹具被删/失效时立刻变红，不然污染是静默回来的。
"""
from pathlib import Path

from singularity.scheduler import config, tracker, witness

REAL_QIDIAN = Path(__file__).resolve().parents[2] / ".qidian"


def _under_real_qidian(p: Path) -> bool:
    return REAL_QIDIAN == p or REAL_QIDIAN in p.parents


def test_alerts_not_written_to_production():
    assert not _under_real_qidian(witness._alerts_path()), witness._alerts_path()


def test_tasks_not_written_to_production():
    assert not _under_real_qidian(tracker.tasks_dir()), tracker.tasks_dir()


def test_warn_actually_lands_somewhere():
    """反向对照：隔离不等于禁用 —— warn 仍应真的写盘，只是写到别处。"""
    witness.warn("test_isolation", "guard_probe")
    hits = [a for a in witness.read_alerts(limit=50) if a.get("msg") == "guard_probe"]
    assert hits, "warn 没落盘 = 隔离把 witness 关掉了，比污染更糟"
    assert not _under_real_qidian(witness._alerts_path())
