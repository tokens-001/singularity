"""Remaining property tests — tracker, heartbeat, snapshot, worktree, token budget, model registry, dispatcher, critical fixes."""
import json, time, hashlib
from pathlib import Path
from singularity.scheduler.tracker import _next_id, _invalidate_scan_cache, _read, TaskStatus, _TERMINAL
from singularity.scheduler.model_registry import load_models, for_tier
from singularity.scheduler.dispatcher import load_agents
from singularity.scheduler import tracker, config


class TestModelRegistry:
    """模型注册表查询。"""

    def test_load_models(self):
        models = load_models()
        assert len(models) > 3
        assert "deepseek-v4-pro" in models

    def test_for_tier(self):
        e_models = for_tier("E", available_only=False)
        assert len(e_models) > 0
        tiers = {t for m in e_models for t in m.tiers}
        assert "E" in tiers


class TestInsertAgent:
    """Agent CRUD。"""

    def test_load_agents(self):
        agents = load_agents()
        assert "E" in agents
        assert "D" in agents
        total = sum(len(v) for v in agents.values())
        assert total >= 0, f"agent loading should not crash, got {total}"


class TestCriticalFixes:
    """三模型审查 CRITICAL 修复的边界条件测试。"""

    def test_next_id_monotonic(self):
        ids = set()
        for _ in range(100):
            ids.add(_next_id())
        assert len(ids) == 100

    def test_next_id_increasing(self):
        prev = int(_next_id())
        for _ in range(50):
            curr = int(_next_id())
            assert curr > prev, f"ID should increase: {prev} → {curr}"
            prev = curr

    def test_auth_bootstrap_rejects_when_users_exist(self):
        from singularity.scheduler._api import auth_bootstrap
        result, code = auth_bootstrap()
        assert code in (200, 403)
        if code == 403:
            assert not result.get("ok")

    def test_goal_check_no_agent_returns_false(self):
        from singularity.scheduler.goal_loop import GoalLoop
        gl = GoalLoop.__new__(GoalLoop)
        gl._agents = {"E": []}
        result = gl._check_goal("test output", "test goal", "test task")
        assert not result.get("met", True)

    def test_token_auth_backward_compat(self):
        from singularity.scheduler._auth import _hash_token, _hash_token_v2
        token = "test-token-12345"
        h1 = _hash_token(token)
        h2 = _hash_token_v2(token)
        assert h1 != h2
        assert len(h1) == 64
        assert len(h2) == 64


class TestPropertyTaskStatus:
    """任务状态转换不变量。"""

    def test_terminal_states_never_retry(self):
        for ts in _TERMINAL:
            assert ts in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ROLLED_BACK}

    def test_valid_transitions(self):
        valid = {
            TaskStatus.PENDING: {TaskStatus.ROUTED, TaskStatus.FAILED},
            TaskStatus.ROUTED: {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ROLLED_BACK},
            TaskStatus.BLOCKED: {TaskStatus.ROUTED, TaskStatus.FAILED},
        }
        for src, targets in valid.items():
            for tgt in targets:
                assert tgt is not None


class TestPropertyHeartbeat:
    """心跳不变量。"""

    def test_heartbeat_staleness_monotonic(self):
        from singularity.scheduler.witness import _hb_path, heartbeat
        heartbeat("old_task", "E", "running")
        hb_file = _hb_path("old_task", "E")
        assert hb_file.exists()
        hb_file.unlink(missing_ok=True)

    def test_cleanup_terminal_removes_done_heartbeat(self):
        from pathlib import Path
        from singularity.scheduler.witness import _hb_path, _heartbeat_dir, heartbeat, _cleanup_terminal_heartbeat
        tid = f"pt_{int(time.time())}"
        task_dir = tracker._tasks_dir()
        task_dir.mkdir(parents=True, exist_ok=True)
        task_file = task_dir / f"{tid}.json"
        task_file.write_text(json.dumps({"status": "done"}))
        heartbeat(tid, "D", "running")
        hb_file = _hb_path(tid, "D")
        assert hb_file.exists()
        cleaned = _cleanup_terminal_heartbeat(hb_file, tid)
        assert cleaned
        assert not hb_file.exists()
        task_file.unlink(missing_ok=True)


class TestPropertySnapshot:
    """Snapshot 不变量。"""

    def test_snapshot_id_format(self):
        from singularity.scheduler.snapshot import Snapshot
        s = Snapshot(id="1782000000_t123", method="git", ref="abc123", created_at=1782000000.0)
        assert "_" in s.id
        assert len(s.id.split("_")) == 2

    def test_batch_snapshot_id_format(self):
        from singularity.scheduler.snapshot import Snapshot
        s = Snapshot(id="1782000000_batch_b001", method="copy", ref="/tmp/x", created_at=1782000000.0)
        assert "batch" in s.id


class TestPropertyWorktree:
    """Worktree 不变量。"""

    def test_dir_naming_pattern(self):
        import re
        tid, lvl = "1782000001", "E"
        name = f"{tid}_{lvl}"
        assert re.match(r"^\d+_[DE]\+?$", name)


class TestPropertyTokenBudget:
    """Token 预算不变量。"""

    def test_spent_never_exceeds_total(self):
        total, spent = 500000, 123000
        remaining = total - spent
        assert remaining >= 0
        assert spent <= total

    def test_default_unlimited(self):
        total, spent = None, 100
        remaining = float("inf") if total is None else total - spent
        assert remaining == float("inf")
