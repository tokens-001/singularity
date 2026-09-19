"""调度循环推进块的三个窄洞（2026-09-19 外派评审 B1/B2/B3）。

三条真机都**没撞过** —— 触发条件窄，所以一直没被发现。写下来比等它撞上再查便宜：
B1 是"烧钱不停"，B2 是"事后分不清哪个 tag 是那次交付"，B3 是"人以为项目在动"。
"""
import subprocess

from singularity.scheduler import orchestrator as orch
from singularity.scheduler import project as proj_mod
from singularity.scheduler.project import Phase


def _mk_project(monkeypatch, phase=Phase.REVIEWING):
    monkeypatch.setattr(proj_mod, "save", lambda _p: None)
    p = proj_mod.ProjectState(id="p1", name="n", phase=phase)
    monkeypatch.setattr(proj_mod, "load", lambda _pid: p)
    return p


# ── B1：验收连续失败必须停手 ────────────────────────────────

def test_验收连续失败到上限就停手(monkeypatch):
    """验收一抛就回到 `reviewing` ⇒ **每个 tick 重跑一整段**（2 次 LLM + 最多 10 条
    子进程检查），而 phase 一直不动 ⇒ 永远不停。

    判据钉在「`run_test_fix_loop` 被调了几次」上：去掉 `verify_attempts` 计数器，
    这里会一直涨（跑到循环结束都停不下来）。
    """
    p = _mk_project(monkeypatch, Phase.REVIEWING)
    monkeypatch.setattr(orch, "_merge_inflight", set())

    from singularity.scheduler import workflow as wf
    calls = []

    def boom(proj, agents):
        calls.append(1)
        raise RuntimeError("验收炸了")

    monkeypatch.setattr(wf, "run_test_fix_loop", boom)

    for _ in range(orch._VERIFY_MAX_ATTEMPTS + 3):
        orch._run_verification_async("p1", {})

    assert len(calls) == orch._VERIFY_MAX_ATTEMPTS, \
        f"超过上限还在重跑验收（一直在烧钱）: {len(calls)} 次"
    assert any(i.get("kind") == "verify_attempts_exhausted" for i in p.issues), \
        f"停手了却没人知道 —— 项目会静静躺着: {p.issues}"


def test_进REVIEWING时计数清零(monkeypatch, tmp_path):
    """`verify_attempts` 量的是"**连续**失败几次"，不是"这个项目一共验过几次"。

    进 REVIEWING = 验收从头再来，必须清零；不清的话第二次打回后一次都跑不了
    （计数早就 ≥ 上限了）。
    """
    p = _mk_project(monkeypatch, Phase.INTEGRATING)
    p.verify_attempts = orch._VERIFY_MAX_ATTEMPTS

    monkeypatch.setattr(orch, "_merge_inflight", set())
    monkeypatch.setattr(proj_mod, "load", lambda _pid: p)
    # 集成合并成功那条路 → set_phase(REVIEWING) 前必须清零
    monkeypatch.setattr(orch, "_run_integration_merge", lambda _p: (True, "合并完成"))
    monkeypatch.setattr(orch, "_pending_sse_events", [])
    from singularity.scheduler import workflow as wf
    monkeypatch.setattr(wf, "run_test_fix_loop", lambda proj, agents: "ok")
    from singularity.scheduler._review import check_review_fail_limit
    monkeypatch.setattr("singularity.scheduler._review.check_review_fail_limit",
                        lambda *a, **k: {"blocked": False})

    orch._run_integration_merge_async("p1", {})

    assert p.phase == Phase.REVIEWING
    assert getattr(p, "verify_attempts", 0) == 0, \
        "进 REVIEWING 没清零 —— 这个项目以后一次验收都跑不了了"


# ── B2：交付重试不能重复打 tag ──────────────────────────────

def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


def test_交付重试不重复打tag(monkeypatch, tmp_path):
    """打完 tag 之后抛出去 ⇒ phase 留在 `delivering` ⇒ 下个 tick 再跑一次。

    tag 名带**到分钟的时间戳** ⇒ 每重试一次多一个 `release/<id>-<分钟>`，
    一串标签指着**同一个 commit**，事后分不清哪个才是"那次交付"。

    判据：先手工放一个"上一次留下的" tag（名字带别的分钟，模拟跨分钟重试），
    再跑一次交付 —— 必须**复用它**，不是再打一个。删掉 `--points-at HEAD` 那段 ⇒ 红。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "tag", "release/p1-202001010000")     # ← 上一次交付留下的

    p = proj_mod.ProjectState(id="p1", name="n", phase=Phase.DELIVERING)
    monkeypatch.setattr(proj_mod, "repo_dir", lambda _pid: repo)

    ok, _detail = orch._run_delivery(p)

    assert ok
    tags = sorted(t for t in _git(repo, "tag", "-l", "release/*").stdout.split())
    assert tags == ["release/p1-202001010000"], f"重试又打了一个 tag: {tags}"


# ── B3：批准之后说清楚"谁在推" ──────────────────────────────

def test_批准后要说清谁在推这个项目(monkeypatch):
    """落到调度循环那几档时，"循环开没开"必须写在返回里。

    `next_phase` 只是个**阶段名**，它不说"有没有人在推"：循环没开时项目停在原地，
    而界面上只显示"实现中"，看不出是没人点火（§28 那个形状）。
    """
    from singularity.scheduler._api_projects import _loop_status
    from singularity.web import app as web_app

    # 归 run_phase 推的档位：没有"循环开没开"这回事
    assert _loop_status(Phase.PLANNING)["driven_by"] == "run_phase"

    monkeypatch.setattr(web_app, "_loop_running", True)
    out = _loop_status(Phase.EXECUTING)
    assert out["driven_by"] == "scheduler_loop" and out["loop_running"] is True
    assert "warning" not in out

    monkeypatch.setattr(web_app, "_loop_running", False)
    out = _loop_status(Phase.EXECUTING)
    assert out["loop_running"] is False
    assert out.get("warning"), "循环没开却不吭声 —— 人以为项目在动"
