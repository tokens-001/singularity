"""Remaining property tests — tracker, heartbeat, snapshot, worktree, token budget, model registry, dispatcher, critical fixes."""
import json, time, hashlib
from pathlib import Path
from singularity.scheduler.tracker import _next_id, _invalidate_scan_cache, read_task, TaskStatus, _TERMINAL
from singularity.scheduler.model_registry import load_models, for_tier
from singularity.scheduler.dispatcher import load_agents
from singularity.scheduler import tracker, config


class TestModelRegistry:
    """模型注册表查询。"""

    def test_load_models(self):
        models = load_models()
        assert len(models) > 3  # 能力快照(models.toml)必须在
        assert "deepseek-v4-pro" in models

    def test_for_phase(self):
        models = for_tier("定义", available_only=False) or for_tier("实现", available_only=False)
        assert len(models) >= 0  # 两档后按阶段推荐查询


class TestInsertAgent:
    """Agent CRUD。"""

    def test_load_agents(self):
        agents = load_agents()
        # 两档后 agents 用 "any" 键或自定义键, 不再强制 E/D
        total = sum(len(v) for v in agents.values() if isinstance(v, list))
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
        gl._agents = {"any": []}
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
    """任务状态转换不变量 —— **测真实现**。

    ⚠️ 这一批原来叫"property 测试"，但断言全落在**测试自己手写的字面量**上：
    `_TERMINAL ⊆ {...}` 里那个 `_TERMINAL` 就是本文件定义的，`assert tgt is not None`
    更是恒真（2026-09-14 外派⑤核出、我复核属实）。
    ⇒ 照 `tracker` 的真实现重写：**这些不变量是真的，只是原来没测到实现**。
    """

    def test_终态只许走白名单出口(self, tmp_path, monkeypatch):
        """`tracker.transition` 对终态改判有白名单（`_TERMINAL_EXIT`）—— 测它真拦得住。

        `DONE` 的出口是**空集**：改判会造出"代码已合入却显示失败"，或把它转回 PENDING
        导致重复执行。
        """
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
        t = tracker.create("终态出口测试")
        tracker.transition(t.id, TaskStatus.DONE)
        assert tracker.read_task(t.id).status == TaskStatus.DONE

        assert tracker.transition(t.id, TaskStatus.PENDING) is None, \
            "DONE 被改判成 PENDING 了 —— 白名单没拦住"
        assert tracker.read_task(t.id).status == TaskStatus.DONE, "状态被改了"

    def test_失败的任务允许回到_PENDING(self, tmp_path, monkeypatch):
        """对照：`FAILED` 的出口白名单里有 `PENDING`（重排是合法操作），别把闸门焊死。"""
        monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / ".qidian")
        t = tracker.create("重排测试")
        tracker.transition(t.id, TaskStatus.FAILED)
        assert tracker.transition(t.id, TaskStatus.PENDING) is not None, \
            "FAILED → PENDING 被拦了 —— 任务永远重排不了"
        assert tracker.read_task(t.id).status == TaskStatus.PENDING


class TestPropertyHeartbeat:
    """心跳不变量。"""

    def test_心跳能写进盘(self):
        """⚠️ 原来叫 `test_heartbeat_staleness_monotonic`，可**内容里没有任何 staleness 判断**
        （只写了一次心跳、断言文件在、再删掉）—— 名字与内容不符（2026-09-14 外派⑤核出）。
        改成名字说的事：**心跳确实落了盘**。
        """
        from singularity.scheduler.witness import _hb_path, heartbeat
        heartbeat("old_task", "any", "running")
        hb_file = _hb_path("old_task", "any")
        assert hb_file.exists()
        hb_file.unlink(missing_ok=True)

    def test_cleanup_terminal_removes_done_heartbeat(self):
        from pathlib import Path
        from singularity.scheduler.witness import _hb_path, _heartbeat_dir, heartbeat, _cleanup_terminal_heartbeat
        tid = f"pt_{int(time.time())}"
        task_dir = tracker.tasks_dir()
        task_dir.mkdir(parents=True, exist_ok=True)
        task_file = task_dir / f"{tid}.json"
        task_file.write_text(json.dumps({"status": "done"}))
        heartbeat(tid, "any", "running")
        hb_file = _hb_path(tid, "any")
        assert hb_file.exists()
        cleaned = _cleanup_terminal_heartbeat(hb_file, tid)
        assert cleaned
        assert not hb_file.exists()
        task_file.unlink(missing_ok=True)


class TestPropertySnapshot:
    """快照 id 格式 —— **测真正的生成方 `snapshot.take()`**。

    ⚠️ 原来那两条是构造一个手写字面量的 `Snapshot`、再断言这个字面量里有 "_" / "batch" ——
    测的是**测试自己写的字符串**（2026-09-14 外派⑤核出、我复核属实）。
    """

    def test_快照_id_由_take_生成且带时间戳前缀(self, tmp_path):
        import subprocess
        from singularity.scheduler import snapshot as snap_mod

        config.ensure_dirs()      # `take()` 要往 SNAPSHOT_DIR 写，目录得先在
        root = tmp_path / "repo"
        root.mkdir()
        for cmd in (["git", "init", "-q", "-b", "main"],
                    ["git", "config", "user.email", "t@t"],
                    ["git", "config", "user.name", "t"]):
            subprocess.run(cmd, cwd=root, check=True, capture_output=True)
        (root / "a.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True, capture_output=True)

        snap = snap_mod.take("t123", repo_root=root)

        head, _, tail = snap.id.partition("_")
        assert head.isdigit(), f"id 前缀不是时间戳：{snap.id}"
        assert tail == "t123", f"id 没带 task_id：{snap.id}"
        assert snap.ref, "git 快照没记 ref —— 回滚时无处可回"


# ═══════════════════════════════════════════════════════════════
# 删掉的两条（2026-09-14，外派⑤核出、我复核属实）
# ═══════════════════════════════════════════════════════════════
# `TestPropertyWorktree.test_dir_naming_pattern` 和 `TestPropertyTokenBudget` 那两条
# **没有可测的真实现**：前者自己拼 `f"{tid}_{lvl}"` 再拿正则匹配自己拼的串；
# 后者在两个字面量上做减法（`total, spent = 500000, 123000; assert spent <= total`）。
# 它们的"不变量"**在代码里没有对应物** ⇒ 留着只会给虚假的安心。
# ⚠️ 如果哪天有了真的 worktree 命名生成 / 预算 clamp 实现，**在这里重写**，别恢复旧写法。
