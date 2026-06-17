"""pytest fixtures — 自动清理测试残留。"""
import os
import sys
from pathlib import Path

import pytest

# 从 tests/ 向上两级到项目根 (python/tests/ → python/ → 奇点/)
_project_root = Path(__file__).resolve().parent.parent.parent
_qidian = _project_root / ".qidian"


@pytest.fixture(autouse=True)
def _clean_test_tasks():
    """每个测试后清理 .qidian/ 下的测试产物。"""
    yield
    for subdir in ("tasks", "traces", "heartbeats", "holds", "cancels"):
        d = _qidian / subdir
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                f.unlink()
            except OSError:
                pass
