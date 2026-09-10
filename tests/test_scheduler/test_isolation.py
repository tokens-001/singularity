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


def test_import_time_paths_also_isolated():
    """**导入时**固定死的路径也得隔离 —— 只改 config.QIDIAN_DIR 覆盖不到它们。

    这些是今晚实际踩到的：`route.level` 修好后路由学习器第一次真写盘，
    不隔离就会往生产 route_learner.json 灌测试样本。
    """
    import pytest
    from singularity.scheduler import config, _memory_core, route_learner

    paths = {
        "config.SNAPSHOT_DIR": config.SNAPSHOT_DIR,
        "config.TRACE_DIR": config.TRACE_DIR,
        "config.PARKED_DIR": config.PARKED_DIR,
        "_memory_core._MEMORY_DIR": _memory_core._MEMORY_DIR,
        "route_learner._LEARNER_PATH": route_learner._LEARNER_PATH,
    }
    bad = {k: str(v) for k, v in paths.items() if _under_real_qidian(v)}
    assert not bad, f"这些还指着生产 .qidian：{bad}"
    # 反向对照：得真的指到临时目录下，不是被改成了别处的常量
    for k, v in paths.items():
        assert str(v).startswith(str(config.QIDIAN_DIR)), (k, str(v))


def test_warn_actually_lands_somewhere():
    """反向对照：隔离不等于禁用 —— warn 仍应真的写盘，只是写到别处。"""
    witness.warn("test_isolation", "guard_probe")
    hits = [a for a in witness.read_alerts(limit=50) if a.get("msg") == "guard_probe"]
    assert hits, "warn 没落盘 = 隔离把 witness 关掉了，比污染更糟"
    assert not _under_real_qidian(witness._alerts_path())
