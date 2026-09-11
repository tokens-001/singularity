"""工具级审批通道（2026-09-11 接上）。

**这条通道原来不存在**：`require_approval` 命中后只往 SSE 推一条
"标记为需审批（当前不阻断，仅通知）" 就照样放行，而 `_api_tasks.task_approval`
也只是推条消息 —— **没有任何执行器读得到它**。所以"审批"是只播报不拦的半成品，
代码注释当时如实写了这点：真拦下去就是死锁，因为全仓没有答复的地方。

现在补上：执行器写请求 → 阻塞轮询 → 人工经 API 落决策 → 取走。

**最要紧的两条**：
  1. **绝不能死锁** —— 等待有上限，超时按拒绝（fail-closed），
     且上限必须小于 orchestrator 的任务超时(900s)，否则任务先被砍、文件留在盘上；
  2. **任何异常都按拒绝** —— 这是门禁，失败方向必须是"不放行"。
"""
import json
import threading
import time

import pytest

from singularity.scheduler import config
from singularity.scheduler import permission as perm


@pytest.fixture
def hold(tmp_path, monkeypatch):
    """隔离 HOLD_DIR（conftest 也会做，这里显式一份，跑单文件时也不写生产目录）。"""
    d = tmp_path / "holds"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "HOLD_DIR", d)
    return d


def _request_in_thread(**kw):
    """request_approval 是阻塞的 —— 放线程里，让本线程能扮演"人"。"""
    out = {}
    def _run():
        out["result"] = perm.request_approval(**kw)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t, out


def _wait_for_request(timeout=5.0):
    """等到请求真的落盘。"""
    end = time.time() + timeout
    while time.time() < end:
        pend = perm.list_pending_approvals()
        if pend:
            return pend[0]
        time.sleep(0.05)
    return None


class TestDecide:
    def test_approve_releases_the_waiter(self, hold):
        t, out = _request_in_thread(task_id="t1", level="any", model="m",
                                    tool_name="run_command", args={"command": "ls"})
        assert _wait_for_request() is not None, "请求没落盘"
        assert perm.decide_approval("t1", "approve") is True
        t.join(timeout=5)
        assert out["result"] == (True, "")

    def test_reject_blocks_with_reason(self, hold):
        t, out = _request_in_thread(task_id="t2", level="any", model="m",
                                    tool_name="write_file", args={})
        assert _wait_for_request() is not None
        perm.decide_approval("t2", "reject")
        t.join(timeout=5)
        allowed, why = out["result"]
        assert allowed is False and "拒绝" in why

    def test_unknown_decision_is_treated_as_reject(self, hold):
        """decision 只认 "approve"，别的词一律拒绝 —— 别让拼写错误变成放行。"""
        t, out = _request_in_thread(task_id="t3", level="any", model="m",
                                    tool_name="run_command", args={})
        assert _wait_for_request() is not None
        perm.decide_approval("t3", "yes")          # 不是 "approve"
        t.join(timeout=5)
        assert out["result"][0] is False

    def test_decide_returns_false_when_nothing_pending(self, hold):
        """点一个已经失效的审批必须说"没找到" —— 不能一律回成功。"""
        assert perm.decide_approval("nope", "approve") is False

    def test_no_double_answer(self, hold):
        t, _ = _request_in_thread(task_id="t4", level="any", model="m",
                                  tool_name="run_command", args={})
        assert _wait_for_request() is not None
        assert perm.decide_approval("t4", "approve") is True
        assert perm.decide_approval("t4", "reject") is False, "同一条不该能答两次"
        t.join(timeout=5)


class TestFailClosed:
    def test_timeout_denies_and_does_not_hang(self, hold):
        """**最要紧的一条**：没人应答时必须在时限内返回"拒绝"，不能永远挂着。"""
        start = time.time()
        allowed, why = perm.request_approval(
            task_id="t5", level="any", model="m", tool_name="run_command",
            args={}, timeout=2)
        elapsed = time.time() - start
        assert allowed is False, "超时不能放行"
        assert "超时" in why
        assert elapsed < 6, f"超时兜底没生效，等了 {elapsed:.1f}s"

    def test_request_file_is_cleaned_up(self, hold):
        """答完/超时都要清理 —— 留着会让界面一直显示一条已失效的待审。"""
        perm.request_approval(task_id="t6", level="any", model="m",
                              tool_name="run_command", args={}, timeout=1)
        assert not (hold / "t6.json").exists()
        assert perm.list_pending_approvals() == []

    def test_removed_request_denies_fast(self, hold):
        """请求文件被删（任务删除 / 人工清理）→ 立刻拒绝，别傻等到超时。"""
        t, out = _request_in_thread(task_id="t7", level="any", model="m",
                                    tool_name="run_command", args={}, timeout=30)
        assert _wait_for_request() is not None
        (hold / "t7.json").unlink()
        t.join(timeout=5)
        assert out["result"][0] is False
        assert "移除" in out["result"][1]

    def test_write_failure_denies(self, hold, monkeypatch):
        """写盘就失败 → 拒绝（门禁失败方向是"不放行"），且原因要说清。"""
        import singularity.scheduler._io as io_mod
        def _boom(*a, **k):
            raise OSError("磁盘满了")
        monkeypatch.setattr(io_mod, "atomic_write_json", _boom)
        allowed, why = perm.request_approval(task_id="t8", level="any", model="m",
                                             tool_name="run_command", args={}, timeout=2)
        assert allowed is False and "拒绝" in why


class TestPendingList:
    def test_decided_requests_are_not_listed(self, hold):
        perm.request_approval(task_id="t9", level="any", model="m",
                              tool_name="run_command", args={}, timeout=1)
        assert perm.list_pending_approvals() == [], "答完/超时后不该还在待审列表里"

    def test_corrupt_file_does_not_break_the_list(self, hold):
        """一个坏文件不能让整个列表挂掉 —— 否则界面什么都看不到。"""
        (hold / "bad.json").write_text("{不是 json", encoding="utf-8")
        (hold / "good.json").write_text(json.dumps(
            {"task_id": "good", "tool": "run_command", "decision": None}),
            encoding="utf-8")
        pend = perm.list_pending_approvals()
        assert [p["task_id"] for p in pend] == ["good"]

    def test_args_preview_is_truncated(self, hold):
        """待审列表别把整个文件内容搬进界面。"""
        long_args = {"content": "x" * 5000}
        t, _ = _request_in_thread(task_id="t10", level="any", model="m",
                                  tool_name="write_file", args=long_args)
        got = _wait_for_request()
        assert got is not None
        assert len(got["args_preview"]) <= 200
        perm.decide_approval("t10", "reject")
        t.join(timeout=5)


class TestOrphanReaping:
    """孤儿请求收尸 —— 真机探测踩出来的，单测原本发现不了。

    正常路径由 `request_approval` 的 finally 删文件，但**进程被 kill** 时
    finally 不会执行（线程是被杀掉的，不是正常退栈）。留下的文件会让界面
    永远挂着一条既点不动、又不会自己消失的幽灵审批。
    """

    def test_stale_pending_is_reaped(self, hold):
        (hold / "dead.json").write_text(json.dumps({
            "task_id": "dead", "tool": "run_command", "decision": None,
            "requested_at": time.time() - perm.APPROVAL_TIMEOUT_SEC - 120,
        }), encoding="utf-8")
        assert perm.list_pending_approvals() == []
        assert not (hold / "dead.json").exists(), "过期条目要顺手删掉，不能只过滤"

    def test_fresh_pending_survives(self, hold):
        """刚写进来的不能被误收 —— 宽限期就是为了不跟正在倒计时的那条抢。"""
        (hold / "live.json").write_text(json.dumps({
            "task_id": "live", "tool": "run_command", "decision": None,
            "requested_at": time.time(),
        }), encoding="utf-8")
        assert [p["task_id"] for p in perm.list_pending_approvals()] == ["live"]

    def test_just_expired_still_listed_inside_grace(self, hold):
        (hold / "edge.json").write_text(json.dumps({
            "task_id": "edge", "tool": "run_command", "decision": None,
            "requested_at": time.time() - perm.APPROVAL_TIMEOUT_SEC - 5,
        }), encoding="utf-8")
        assert [p["task_id"] for p in perm.list_pending_approvals()] == ["edge"]
