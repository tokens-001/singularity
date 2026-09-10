"""超时必须真的超时 —— 不能退出时 join 把调用拖住（2026-09-11 审计）。

**这个 bug**：`with concurrent.futures.ThreadPoolExecutor(...) as ex:` 配
`fut.result(timeout=N)` / `wait(timeout=N)` —— 超时确实抛/返回了，但**退出 with 块**时
`shutdown(wait=True)` 会去 join 那些没跑完的线程，于是 N 只是"延迟判定"而不是时限。

实测：`timeout=1s` 的包装套一个 3s 的调用，with 块 3.0s 才退出。
这是 A/B 探针三次卡死的直接原因（模型调用挂住 → 整条流水线跟着挂）。

修法：显式 `ex.shutdown(wait=False)`，不 join。

**在旧代码上会红、且红得对**（断言失败）：下面两条都会因耗时 ≈ 底层 sleep 而失败。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import singularity.scheduler.dispatcher as _d                # noqa: E402,F401
from singularity.scheduler import _dispatch_exec as de        # noqa: E402
from singularity.scheduler import _review as rv               # noqa: E402
from singularity.scheduler import validator as val            # noqa: E402


class TestCommitteeDoesNotJoinHungMembers:
    """委员会里某个模型挂死时，波超时到点就该带着已完成的部分返回。"""

    def test_returns_at_wave_timeout_not_at_slowest_member(self, monkeypatch):
        from singularity.scheduler import execution_judge as ej

        def fake(agent_cfg, prompt, tag, level, baseline_ref="", cwd=""):
            if agent_cfg.get("model") == "slow":
                time.sleep(3)
            return ('{"architecture":"x"}', 100, 1.0)

        monkeypatch.setattr(de, "_run_no_tools", fake)
        monkeypatch.setattr(ej, "_is_architecture_task", lambda t: True)
        monkeypatch.setattr(ej, "fuse_architecture_v2", lambda *a, **k: '{"architecture":"fused"}')
        monkeypatch.setattr(de, "_WAVE_TIMEOUT", 0.5)
        monkeypatch.setattr(de.witness, "warn", lambda *a, **k: None)

        t0 = time.time()
        de._dispatch_committee("模块划分 数据模型", "any", "tid", {},
                               [{"model": "fast"}, {"model": "slow"}])
        elapsed = time.time() - t0

        assert elapsed < 2.0, f"波超时 0.5s，却等了 {elapsed:.2f}s —— 退出时被 join 拖住了"


class TestReviewDoesNotJoinHungTestRunner:
    """项目测试挂死时，审查超时到点就该返回并记 unverified。"""

    def test_returns_at_review_timeout(self, monkeypatch):
        monkeypatch.setattr(val, "run_project_tests", lambda cwd=None: (time.sleep(3), "")[1])
        monkeypatch.setattr(rv, "_REVIEW_TIMEOUT_SEC", 0.5)
        monkeypatch.setattr(rv, "_is_trivial_change", lambda *a, **k: False)

        class V:
            action = "pass"
            unverified = []

        q = {"warnings": [], "confidence": 0.5, "quality_signals": {}}
        t0 = time.time()
        rv.run_post_exec_checks(
            validation=V(), quality=q, exec_result=None,
            task=type("T", (), {"project_id": "", "description": "d"})(),
            agent_cfg={"model": "w"}, level="any", cwd="/tmp", changed=["a.py"])
        elapsed = time.time() - t0

        assert elapsed < 2.0, f"审查超时 0.5s，却等了 {elapsed:.2f}s"
        assert any("超时" in u for u in V.unverified), "超时了却没记 unverified"


class TestParallelHandlesEmpty:
    def test_empty_thunks_do_not_raise(self):
        from singularity.scheduler.execution_judge import _parallel
        assert _parallel([]) == []
