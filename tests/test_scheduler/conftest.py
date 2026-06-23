"""conftest.py — ponytail: session-level cleanup of test artifacts."""
import pytest
import glob, os
from singularity.scheduler import config


@pytest.fixture(autouse=True)
def _cleanup_task_files():
    """每个测试后清理 tracker 创建的残留任务文件。"""
    yield
    try:
        for f in glob.glob(str(config.QIDIAN_DIR / "tasks" / "*.json")):
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception:
        pass
