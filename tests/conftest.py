"""共享 fixtures — Singularity 单元测试。"""
import os, tempfile, pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_qidian_dir(tmp_path, monkeypatch):
    """把 `config.QIDIAN_DIR` 及**所有从它派生的路径**指到临时目录。

    以前不隔离：被测代码里的 witness.warn / tracker 落盘会真写生产目录，
    实测整套测试往 `.qidian/alerts.jsonl` 追加 12 行（跑测试污染排查数据 ——
    alerts.jsonl 正是拿来查故障的，混进测试噪声就废了）。

    **只改 QIDIAN_DIR 是不够的** —— 下面这些在**导入时**就算好了，属性改了它们不变，
    用到它们的代码照样写真实目录（`route_learner` 尤其要紧：`route.level` 修好后它
    才第一次真正记数据，不隔离就会往生产的 route_learner.json 里灌测试样本）：
      · config.{SNAPSHOT,PATCH,TRACE,HOLD,CANCEL,PAUSE,PARKED}_DIR
      · _memory_core._MEMORY_DIR
      · route_learner._LEARNER_PATH
    """
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    for name in ("SNAPSHOT_DIR", "PATCH_DIR", "TRACE_DIR", "HOLD_DIR",
                 "CANCEL_DIR", "PAUSE_DIR", "PARKED_DIR"):
        monkeypatch.setattr(config, name, tmp_path / getattr(config, name).name)

    from singularity.scheduler import _memory_core, route_learner
    monkeypatch.setattr(_memory_core, "_MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(route_learner, "_LEARNER_PATH", tmp_path / "route_learner.json")


@pytest.fixture
def tmp_project_root(tmp_path):
    """提供隔离的项目根目录。"""
    old = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(old)


@pytest.fixture
def tmp_workdir():
    """提供临时工作目录 (含文件写入)。"""
    d = tempfile.TemporaryDirectory()
    yield d.name
    d.cleanup()


def make_task(tid, priority=0, starvation=0, level="any", children=None):
    """快捷构造 task 对象。"""
    return type("T", (), {
        "id": tid,
        "priority": priority,
        "starvation_score": starvation,
        "route_level": level,
        "children": children or [],
        "status": "pending",
    })()
