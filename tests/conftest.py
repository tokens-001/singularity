"""共享 fixtures — Singularity 单元测试。"""
import os, tempfile, pytest
from pathlib import Path


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
