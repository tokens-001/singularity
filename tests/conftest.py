"""共享 fixtures — Singularity 单元测试。"""
import os, tempfile, pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_qidian_dir(tmp_path, monkeypatch):
    """把 config.QIDIAN_DIR 指到临时目录，别让测试写进生产 `.qidian/`。

    以前不隔离：被测代码里的 witness.warn / tracker 落盘会真写生产目录，
    实测整套测试往 `.qidian/alerts.jsonl` 追加 12 行（跑测试污染排查数据 ——
    alerts.jsonl 正是拿来查故障的，混进测试噪声就废了）。

    只改 config.QIDIAN_DIR 这一个属性就能覆盖绝大多数模块，因为它们写的是
    `config.QIDIAN_DIR / "x"`（调用时取属性）。少数在导入时就固定死的
    （如 _memory_core._MEMORY_DIR、config.SNAPSHOT_DIR）本套测试没走到 ——
    哪天走到了，症状是"测试往真实目录写"，就在那里补一行。
    """
    from singularity.scheduler import config
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)


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
