"""conftest.py — ponytail: session-level cleanup of test artifacts."""
import pytest
import glob, os
from singularity.scheduler import config


@pytest.fixture(autouse=True)
def _cleanup_task_files():
    """每个测试后清理测试创建的残留任务文件（只删空文件/无效JSON）。"""
    yield
    try:
        import json as _json
        for f in glob.glob(str(config.QIDIAN_DIR / "tasks" / "*.json")):
            try:
                # ponytail: 只删测试残留的空/无效文件，不删生产任务
                content = open(f).read().strip()
                if not content:
                    os.remove(f)
                    continue
                d = _json.loads(content)
                # 只删测试任务（描述含 "test" 且无真实项目ID的）
                desc = d.get("description", "")
                pid = d.get("project_id", "")
                if "test" in desc.lower() and not pid:
                    os.remove(f)
            except (_json.JSONDecodeError, OSError):
                pass
    except Exception:
        pass
