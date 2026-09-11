"""共享 fixtures — Singularity 单元测试。"""
import os, tempfile, pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_qidian_dir(tmp_path, monkeypatch):
    """把 `config.QIDIAN_DIR` 及**所有从它派生的路径**指到临时目录。

    以前不隔离：被测代码里的 witness.warn / tracker 落盘会真写生产目录，
    实测整套测试往 `.qidian/alerts.jsonl` 追加 12 行（跑测试污染排查数据 ——
    alerts.jsonl 正是拿来查故障的，混进测试噪声就废了）。

    **只改 QIDIAN_DIR 是不够的** —— `config` 里那几个派生目录在**导入时**就算好了，
    属性改了它们不变，用到它们的代码照样写真实目录：
      · config.{SNAPSHOT,PATCH,TRACE,HOLD,CANCEL,PAUSE,PARKED}_DIR
    （内存模块与 `route_learner` 原来也要在这里单独补一刀，**2026-09-11 已改成
    读时现算**，见各自的 `_events_path()` / `_learner_path()` —— 补丁撤了。）
    """
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    for name in ("SNAPSHOT_DIR", "PATCH_DIR", "TRACE_DIR", "HOLD_DIR",
                 "CANCEL_DIR", "PAUSE_DIR", "PARKED_DIR"):
        monkeypatch.setattr(config, name, tmp_path / getattr(config, name).name)


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
