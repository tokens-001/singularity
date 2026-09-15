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
        """按阶段推荐查询要**真返回对的人**。

        ⚠️ 原来只断言 `len(models) >= 0` —— **`len()` 永远 ≥ 0** ⇒
        把 `for_tier` 整个函数体删空、恒返回 `[]` 也照样绿（2026-09-14 读到即坐实）。
        """
        models = for_tier("定义", available_only=False) or for_tier("实现", available_only=False)
        assert models, "按阶段推荐查询返回空 —— 这个查询形同虚设"
        assert all(("定义" in m.recommended_for) or ("实现" in m.recommended_for)
                   for m in models), "返回了跟这两个阶段都不沾边的模型"


class TestInsertAgent:
    """Agent CRUD。"""

    def test_load_agents(self):
        """钉**形状**，别钉"非空"。

        ⚠️ 原来断言 `total >= 0` —— 计数永远 ≥ 0 ⇒ `load_agents` 恒返回 `{}` 也绿。
        ⚠️ 但也**不能改成"必须非空"**：conftest 把 `QIDIAN_DIR` 隔离到 tmp，
        `agents.toml` 根本不在那儿，读出来本来就是 0 个（我第一版改错、当场跑红）。
        ⇒ 钉"返回的是 dict、每个值都是 list"这个**契约**：返回 None 或结构走样都会红。
        """
        agents = load_agents()
        assert isinstance(agents, dict), f"load_agents 该返回 dict，实得 {type(agents)}"
        bad = {k: type(v).__name__ for k, v in agents.items() if not isinstance(v, list)}
        assert not bad, f"这些层级的 agent 配置不是 list: {bad}"


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
        """`code in (200, 403)` 两边都收 —— 得**按分支各断言各的**，否则等于没断。

        ⚠️ 原来只有 `assert code in (200, 403)`：把「已有用户就拒」那句守卫删掉、
        恒走 200，这条**照样绿**（2026-09-14 读到即坐实）。
        """
        from singularity.scheduler._api import auth_bootstrap
        result, code = auth_bootstrap()
        assert code in (200, 403)
        if code == 403:
            assert not result.get("ok")
            assert result.get("error"), "拒了却不说为什么"
        else:
            assert result.get("token"), "建了用户却没把 token 交出来"

    def test_goal_check_no_agent_returns_false(self):
        from singularity.scheduler.goal_loop import GoalLoop
        gl = GoalLoop.__new__(GoalLoop)
        gl._agents = {"any": []}
        result = gl._check_goal("test output", "test goal", "test task")
        assert not result.get("met", True)
        # ⚠️ 只断言 `met` 为假是不够的（2026-09-14 变异坐实）：删掉 `if not e_agents`
        # 那道守卫，下一行 `e_agents[0]` 会抛 IndexError，**被函数末尾那个宽
        # `except Exception` 吞成同样形状的 `{"met": False}`** ⇒ 照样绿。
        # 钉住 **reason** 才分得开"走了无 agent 分支"和"兜底兜住了异常"。
        assert "no_judge_agent" in result.get("reason", ""), \
            f"没走到'无 agent'那条分支（被兜底 except 顶替了？）: {result}"

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


# ═══════════════════════════════════════════════════════════════
# `@timed` 失败时必须记下**为什么**（2026-09-15 真机）
# ═══════════════════════════════════════════════════════════════
# 现场：一次 `dispatch` 抛了，事件流里只剩
# `{"event":"fn_fail","fn":"dispatch","elapsed_ms":42416}` ——
# `/tmp/qidian.log` 里**没有 traceback**、`alerts.jsonl` 里**没有对应告警**
# ⇒ **"哪个 dispatch 失败了、为什么"查不出来**。
# 异常在这层 `raise` 出去、被上游接住降级重试，**失败原因就这么没了**。

def test_timed_失败要记下异常类型和消息():
    """`fn_fail` 事件里要带 `error=<类型>: <消息>` —— 不然失败原因追不回来。

    ⚠️ 直接给 `scheduler` logger 挂捕获 handler，**不用 caplog** ——
    `log._log` 是 `propagate = False`，caplog 挂在 root 上根本收不到。
    """
    import logging
    from singularity.scheduler import log as logmod
    from singularity.scheduler.log import timed

    recs = []

    class _H(logging.Handler):
        def emit(self, record):
            recs.append(record)

    h = _H()
    logmod._log.addHandler(h)
    try:
        @timed(name="probe")
        def boom():
            raise ValueError("仓库没找到")

        try:
            boom()
            raise AssertionError("没抛出来 —— 这个桩写错了")
        except ValueError:
            pass
    finally:
        logmod._log.removeHandler(h)

    events = []
    for r in recs:
        try:
            events.append(json.loads(r.getMessage()))
        except (ValueError, TypeError):
            continue
    fails = [e for e in events if e.get("event") == "fn_fail"]
    assert fails, f"没记 fn_fail 事件（收到 {[e.get('event') for e in events]}）"
    err = str(fails[-1].get("error", ""))
    assert "ValueError" in err, f"只记了'失败了'，没记异常类型：{fails[-1]}"
    assert "仓库没找到" in err, f"没记异常消息：{fails[-1]}"
