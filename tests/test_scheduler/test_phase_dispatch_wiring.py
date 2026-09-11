"""阶段调用的两条接线：**运行目录**和**禁工具**。

两条都是 2026-09-11 探路项目实测出来的，症状不同、坏法一样：`_safe_dispatch`
没把参数传下去，执行器各自兜底到最危险的默认值。

1. **cwd 漏传** → 执行器兜底 `config.PROJECT_ROOT` = 奇点仓库自己。调研员带着
   写文件/跑命令的工具，在**主仓根目录**写下 `wc_lite.py` + `examples/`。
   （同一形状此前已在委员会合成那条支路上踩过 —— 当时只修了那一处，根因没动。）
2. **没禁工具** → 调研员把"调研"当成"实现"，在项目仓库里把整个项目写完，
   5 个工具轮次耗尽，报告只剩 "(达到最大工具轮次, 已产出文件)" → GATE1 无物可审。
"""
from singularity.scheduler import config
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import project as proj_mod
from singularity.scheduler import workflow


def _mk_project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "get_projects_root", lambda: tmp_path / "projects")
    p = proj_mod.ProjectState(
        id="proj1", name="探路", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    )
    proj_mod.save(p)   # repo_dir() 是靠 load() 从盘上算路径的，不存盘就走兜底分支
    return p


def test_phase_cwd_is_project_repo_not_qidian(tmp_path, monkeypatch):
    p = _mk_project(tmp_path, monkeypatch)
    cwd = workflow._phase_cwd(p)
    assert cwd == str(tmp_path / "projects" / "探路"), cwd
    assert cwd != str(config.PROJECT_ROOT), "退回奇点仓库 = 调研员会往主仓写文件"
    assert (tmp_path / "projects" / "探路" / ".git").exists(), "仓库没就位，cwd 指向空壳"


def test_phase_cwd_never_falls_back_to_qidian_repo(tmp_path, monkeypatch):
    """项目 JSON 丢了（被删/损坏）也不许退回奇点仓库 —— 那是**主仓**。

    `repo_dir()` 的兜底路径落在 `.qidian/projects/<id>/repo`，丑但安全；
    要守的不变量只有一条：**绝不是 config.PROJECT_ROOT**。
    """
    _mk_project(tmp_path, monkeypatch)
    proj_mod._path("proj1").unlink()          # 项目定义消失
    cwd = workflow._phase_cwd(proj_mod.ProjectState(
        id="proj1", name="探路", raw_constraints=[], owner_confirm={},
        constraints_checklist=[], task_ids=[], issues=[], supervision_log=[],
        lineage=[], handoffs=[], agent_lineup={},
    ))
    assert cwd != str(config.PROJECT_ROOT), "兜底落到奇点仓库 = P0 复活"


def test_safe_dispatch_passes_cwd(tmp_path, monkeypatch):
    """真正的回归点：`_safe_dispatch` 必须把 cwd 传下去。

    旧代码不传 → `dispatch(cwd="")` → 执行器兜底成 `config.PROJECT_ROOT`。
    """
    p = _mk_project(tmp_path, monkeypatch)
    seen = {}

    def fake_dispatch(task, level, task_id, agents, **kw):
        seen.update(kw)
        raise RuntimeError("stop")  # 只关心传参，不真跑模型

    monkeypatch.setattr(disp_mod, "dispatch", fake_dispatch)
    workflow._safe_dispatch("prompt", "any", "t1", {}, p)

    assert seen.get("cwd"), "cwd 没传 → 落到奇点仓库根"
    assert seen["cwd"] == str(tmp_path / "projects" / "探路")


# ── 第二条接线：产出是 JSON 的阶段必须禁工具 ──────────────────────

def _stub_phase_pipeline(monkeypatch, raw: str):
    """把 `_workflow_phases` 的 dispatch 换掉，只记参数、回一段假产出。"""
    from singularity.scheduler import _workflow_phases as wp

    calls = []

    class _FakeER:
        def __init__(self):
            self.raw_output = raw

    class _FakeDisp:
        def __init__(self):
            self.executor_result = _FakeER()
            self.agent_cfg = {"model": "fake-model"}

    def fake_safe_dispatch(prompt, level, task_id, agents, project, lineup=None,
                           restrict=False, phase="", no_tools=False):
        calls.append({"phase": phase, "no_tools": no_tools})
        return _FakeDisp(), ""

    monkeypatch.setattr(wp, "_safe_dispatch", fake_safe_dispatch)
    monkeypatch.setattr(wp, "_save_phase_output", lambda *a, **k: None)
    monkeypatch.setattr(wp, "_phase_selection", lambda phase, project: (None, False))
    return wp, calls


def test_research_disables_tools(tmp_path, monkeypatch):
    """调研阶段禁工具。不禁 → 它把工具轮次全花在写代码上，报告一句不剩。"""
    p = _mk_project(tmp_path, monkeypatch)
    wp, calls = _stub_phase_pipeline(monkeypatch, '{"competitive_analysis": {"products": []}}')
    wp._run_research(p, {})

    assert calls, "调研没走到 dispatch？"
    assert calls[0]["no_tools"] is True, "调研没禁工具 → 会当实现任务干、烧光轮次"


def test_planning_disables_tools(tmp_path, monkeypatch):
    """架构阶段禁工具（委员会那条路本就禁；这条管的是单模型兜底）。"""
    p = _mk_project(tmp_path, monkeypatch)
    arch = '{"tasks": [{"id": "t1", "title": "x", "desc": "y"}], "constraints": []}'
    wp, calls = _stub_phase_pipeline(monkeypatch, arch)
    wp._run_planning(p, {})

    assert calls, "架构没走到 dispatch？"
    assert all(c["no_tools"] is True for c in calls), "架构有调用没禁工具"


# ── 第三条接线：阶段用量必须记进**项目账** ────────────────────────

class _FakeExecResult:
    token_count = 1234
    elapsed = 5.0


class _FakeDispatchResult:
    executor_result = _FakeExecResult()
    agent_cfg = {"model": "fake-model"}


def test_phase_usage_recorded_under_project(tmp_path, monkeypatch):
    """阶段调用要带 project_id 记账。

    不带 → 项目花费恒 0（`project_cost` 按 project_id 查）、预算无从比对、
    这些调用全挤进 `_unknown` 桶。2026-09-11 实测：三次完整调研贡献 0 条记录。
    """
    p = _mk_project(tmp_path, monkeypatch)
    from singularity.scheduler import _token_budget as tb

    rows = []
    monkeypatch.setattr(tb, "record_tokens", lambda **kw: rows.append(kw))
    monkeypatch.setattr(disp_mod, "dispatch", lambda *a, **kw: _FakeDispatchResult())

    workflow._safe_dispatch("prompt", "any", "research_proj1", {}, p)

    assert len(rows) == 1, f"该记一条，实际 {len(rows)} 条"
    assert rows[0]["project_id"] == p.id, "没带 project_id → 又进 _unknown 桶"
    assert rows[0]["task_id"] == "research_proj1"
    assert rows[0]["tokens"] == 1234
    assert rows[0]["model"] == "fake-model"


def test_committee_usage_recorded_per_member(tmp_path, monkeypatch):
    """委员会要**按成员逐个记**，不能用合成名记一条。

    合成名 `fusion(glm-5.3-flash,deepseek-flash)` 在计价表里查不到 →
    费用按 None 跳过 → 项目 cost 恒 $0.0000（2026-09-11 探路轮实测）。
    per-member 的 token 本来就有（_dispatch_committee 里以前直接扔了）。
    """
    p = _mk_project(tmp_path, monkeypatch)
    from singularity.scheduler import _token_budget as tb

    rows = []
    monkeypatch.setattr(tb, "record_tokens", lambda **kw: rows.append(kw))

    class _ER:
        token_count = 300
        elapsed = 9.0
        member_usage = [{"model": "m-a", "tokens": 120, "elapsed": 4.0},
                        {"model": "m-b", "tokens": 180, "elapsed": 5.0}]

    class _D:
        executor_result = _ER()
        agent_cfg = {"model": "fusion(m-a,m-b)"}

    monkeypatch.setattr(disp_mod, "dispatch", lambda *a, **kw: _D())
    workflow._safe_dispatch("prompt", "any", "architect_1", {}, p)

    assert [r["model"] for r in rows] == ["m-a", "m-b"], "必须按成员记，不能记合成名"
    assert [r["tokens"] for r in rows] == [120, 180], "每个成员的用量要各归各"
    assert all(r["project_id"] == p.id for r in rows)


def test_phase_usage_zero_tokens_not_recorded(tmp_path, monkeypatch):
    """0 token 不记 —— 免得用量表被一堆空行撑满。"""
    p = _mk_project(tmp_path, monkeypatch)
    from singularity.scheduler import _token_budget as tb

    rows = []
    monkeypatch.setattr(tb, "record_tokens", lambda **kw: rows.append(kw))

    class _Zero(_FakeDispatchResult):
        class executor_result:
            token_count = 0
            elapsed = 0.0

    monkeypatch.setattr(disp_mod, "dispatch", lambda *a, **kw: _Zero())
    workflow._safe_dispatch("prompt", "any", "t", {}, p)
    assert rows == []


def test_executing_phases_keep_tools(tmp_path, monkeypatch):
    """别的阶段不许跟着禁 —— 实现阶段要的就是工具。"""
    p = _mk_project(tmp_path, monkeypatch)
    seen = {}

    def fake_dispatch(task, level, task_id, agents, **kw):
        seen.update(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(disp_mod, "dispatch", fake_dispatch)
    workflow._safe_dispatch("prompt", "any", "t1", {}, p)
    assert seen.get("no_tools") is False, "默认必须是有工具"

