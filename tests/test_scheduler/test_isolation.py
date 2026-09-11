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
    """`config` 里那几个**导入时**派生好的目录也得隔离（夹具会给它们单独补刀）。"""
    from singularity.scheduler import config

    paths = {
        "config.SNAPSHOT_DIR": config.SNAPSHOT_DIR,
        "config.TRACE_DIR": config.TRACE_DIR,
        "config.PARKED_DIR": config.PARKED_DIR,
    }
    bad = {k: str(v) for k, v in paths.items() if _under_real_qidian(v)}
    assert not bad, f"这些还指着生产 .qidian：{bad}"
    for k, v in paths.items():
        assert str(v).startswith(str(config.QIDIAN_DIR)), (k, str(v))


def test_derived_paths_are_computed_at_call_time():
    """**读时现算**（防御模式 #34）：改完 `config.QIDIAN_DIR`，派生路径要跟着变。

    这几处原来是模块级常量（`_MEMORY_DIR = config.QIDIAN_DIR / "memory"`），
    导入时算死 —— 只 monkeypatch `QIDIAN_DIR` 覆盖不到它们，于是测试往**真实**的
    `.qidian/memory/` 里写（2026-09-11 实测：一条测试把假洞察写进了生产的
    `insights.json`）。2026-09-11 全改成函数，这条钉住别退回去。

    **为什么单列一条**：这类 bug 的默认表现是"测试静默写进生产文件"，
    不做反向对照根本发现不了 —— 加了常量回退没人会注意到。
    """
    from singularity.scheduler import config, _memory_core, _memory_lifecycle
    from singularity.scheduler import _memory_experience, route_learner

    checks = {
        "_memory_core._memory_dir": _memory_core._memory_dir,
        "_memory_core._events_path": _memory_core._events_path,
        "_memory_core._edges_path": _memory_core._edges_path,
        "_memory_core._entity_idx_path": _memory_core._entity_idx_path,
        "_memory_lifecycle._insights_path": _memory_lifecycle._insights_path,
        "_memory_experience._experiences_path": _memory_experience._experiences_path,
        "_memory_experience._failure_patterns_path": _memory_experience._failure_patterns_path,
        "route_learner._learner_path": route_learner._learner_path,
    }
    for name, fn in checks.items():
        p = fn()
        assert not _under_real_qidian(p), f"{name}() 指到生产了: {p}"
        assert str(p).startswith(str(config.QIDIAN_DIR)), (name, str(p))


def test_warn_actually_lands_somewhere():
    """反向对照：隔离不等于禁用 —— warn 仍应真的写盘，只是写到别处。"""
    witness.warn("test_isolation", "guard_probe")
    hits = [a for a in witness.read_alerts(limit=50) if a.get("msg") == "guard_probe"]
    assert hits, "warn 没落盘 = 隔离把 witness 关掉了，比污染更糟"
    assert not _under_real_qidian(witness._alerts_path())
