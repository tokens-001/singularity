"""流程重量分级（窟窿 #1）—— 判据、转发、透传、记账。

背景：重流程其实是**两个独立开关**，不是一个（docs/生产流现状.md 窟窿 #1）：
  ① 6 维度调研（跳 RESEARCHING + GATE1）
  ② 架构多模型委员会（`_is_architecture_task` 管，**贵的那个**）
架构 prompt 本身含「模块划分/数据模型/架构方案」→ ② 恒真，只改 ① 碰不到贵的那半。

本文件钉四件事：
1. `resolve_flow` 的**契约** —— 尤其"auto 永不否决委员会"这条 fail-closed 底线
2. `_needs_research` 只是**转发**，不许再长出自己的关键词表（防御模式 §5）
3. `_safe_dispatch` 真把 `allow_committee` 传下去了（同 test_phase_dispatch_wiring 的形状）
4. 立项时**留下带理由的痕迹**（§44：跳过不能是"没发生"）
"""
from singularity.scheduler import config
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import project as proj_mod
from singularity.scheduler import workflow
from singularity.scheduler.project import (
    FlowDecision, Phase, ProjectState, resolve_flow, suggest_flow,
)


def _mk(**kw) -> ProjectState:
    """纯内存项目 —— 判据是纯函数，不需要落盘。"""
    return ProjectState(id="x", name="n", **kw)


def _mk_on_disk(tmp_path, monkeypatch, **kw) -> ProjectState:
    """要跑 `start_project_workflow` 的用这个：它内部会 `save()`。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "get_projects_root", lambda: tmp_path / "projects")
    p = ProjectState(id="proj1", name="探路", **kw)
    proj_mod.save(p)
    return p


# ── ① 判据矩阵 ──────────────────────────────────────────────

def test_light_两样都省():
    d = resolve_flow(_mk(flow_weight="light"))
    assert (d.research, d.committee) == (False, False)
    assert d.weight == "light" and d.source == "user"


def test_heavy_两样都跑():
    d = resolve_flow(_mk(flow_weight="heavy"))
    assert (d.research, d.committee) == (True, True)
    assert d.weight == "heavy" and d.source == "user"


def test_auto_下_bug_fix_免调研但委员会照开():
    """⚠️ **这条断言就是 fail-closed 契约。**

    auto 只决定调研，永不否决委员会 —— 委员会是贵的那半，凭中文子串猜
    "这活小"就砍掉它正是 §47 禁的那类静默失败。将来谁把 auto 悄悄"优化"
    成轻量，这条会响。
    """
    d = resolve_flow(_mk(template="bug_fix"))
    assert d.research is False
    assert d.committee is True
    assert d.weight == "heavy", "重量是按'两样都不做才算轻'算的"


def test_auto_下_模板命中走调研():
    for tpl in ("product_dev", "agent_dev", "refactor"):
        d = resolve_flow(_mk(template=tpl))
        assert d.research is True and d.committee is True, tpl


def test_auto_下_真实UI形态_描述短且无触发词():
    """前端只给 feature/bugfix/test/review —— 走不到模板分支，全靠关键词。"""
    d = resolve_flow(_mk(template="feature", description="写个把 markdown 转 pdf 的小工具"))
    assert d.research is False
    assert d.committee is True, "描述短不等于可以省掉委员会"


def test_auto_下_描述命中触发词():
    d = resolve_flow(_mk(template="feature", description="先定架构方案再动手"))
    assert d.research is True
    assert "架构" in d.reason


def test_非法值当_auto_处理且不抛异常():
    for bad in ("banana", "", None):
        d = resolve_flow(_mk(flow_weight=bad))
        assert d.source == "auto" and d.committee is True, bad


def test_老项目文件没有该键也能读():
    """存量项目文件里没有 `flow_weight` —— 读出来必须等价于改动前的行为。"""
    p = ProjectState.from_dict({"id": "a", "name": "b", "phase": "template"})
    assert p.flow_weight == "auto"
    assert p.template == "product_dev", "缺 template 落回 dataclass 默认值"
    assert resolve_flow(p).research is True, "product_dev → 调研（与改动前一致）"

    q = ProjectState.from_dict({"id": "a", "name": "b", "phase": "template",
                                "template": "bug_fix"})
    assert resolve_flow(q).research is False, "bug_fix → 免调研（与改动前一致）"
    assert resolve_flow(q).committee is True, "但委员会照开"


# ── ② 转发一致性（§5 单一入口）────────────────────────────────

def test_needs_research_只是转发():
    """`_needs_research` 不许再长出自己的关键词表 —— 两个入口必须同源。"""
    cases = [
        _mk(flow_weight="light"),
        _mk(flow_weight="heavy"),
        _mk(template="bug_fix"),
        _mk(template="product_dev"),
        _mk(template="feature", description="写个小工具"),
        _mk(template="feature", description="先定架构方案"),
        _mk(flow_weight="banana"),
    ]
    for p in cases:
        assert workflow._needs_research(p) == resolve_flow(p).research, p.flow_weight


# ── ③ _safe_dispatch 真的传下去了 ────────────────────────────

def _capture_dispatch(monkeypatch) -> dict:
    seen = {}

    def fake_dispatch(task, level, task_id, agents, **kw):
        seen.update(kw)
        raise RuntimeError("stop")   # 只关心传参，不真跑模型

    monkeypatch.setattr(disp_mod, "dispatch", fake_dispatch)
    return seen


def test_safe_dispatch_轻量项目传_false(tmp_path, monkeypatch):
    p = _mk_on_disk(tmp_path, monkeypatch, flow_weight="light")
    seen = _capture_dispatch(monkeypatch)
    workflow._safe_dispatch("prompt", "any", "t1", {}, p)
    assert seen.get("allow_committee") is False


def test_safe_dispatch_重量项目传_true(tmp_path, monkeypatch):
    p = _mk_on_disk(tmp_path, monkeypatch, flow_weight="heavy")
    seen = _capture_dispatch(monkeypatch)
    workflow._safe_dispatch("prompt", "any", "t1", {}, p)
    assert seen.get("allow_committee") is True


def test_safe_dispatch_显式覆盖优先(tmp_path, monkeypatch):
    p = _mk_on_disk(tmp_path, monkeypatch, flow_weight="heavy")
    seen = _capture_dispatch(monkeypatch)
    workflow._safe_dispatch("prompt", "any", "t1", {}, p, allow_committee=False)
    assert seen.get("allow_committee") is False


# ── ④ 委员会闸门（纯函数，用普通 list 当 chain）────────────────

def test_committee_allowed_四条件():
    from singularity.scheduler._dispatch_exec import _committee_allowed
    arch = "请给出模块划分与技术栈"
    assert _committee_allowed(arch, ["m1", "m2"], "", False) is False, "轻量项目必须挡住"
    assert _committee_allowed(arch, ["m1", "m2"], "", True) is True
    assert _committee_allowed(arch, ["m1"], "", True) is False, "候选不足两个"
    assert _committee_allowed("写个函数", ["m1", "m2"], "", True) is False, "非架构任务"
    assert _committee_allowed(arch, ["m1", "m2"], "implementer", True) is False, "角色否决优先"


# ── ⑤ 立项留痕（§44 三态：跳过必须带理由）─────────────────────

def test_立项_轻量_直接进架构且留下理由(tmp_path, monkeypatch):
    p = _mk_on_disk(tmp_path, monkeypatch, template="feature",
                    description="先定架构方案", flow_weight="light")
    monkeypatch.setattr(workflow, "run_phase", lambda proj, agents: "stub")

    workflow.start_project_workflow(p, {})

    assert p.phase == Phase.PLANNING
    assert not any(e.get("to") == "researching" for e in p.lineage), "不该经过调研"
    fw = [e for e in p.lineage if e.get("action") == "flow_weight"]
    assert len(fw) == 1, p.lineage
    assert fw[0]["research"] is False
    assert fw[0]["committee"] is False
    assert fw[0]["source"] == "user"
    assert fw[0]["reason"], "理由不能是空的 —— 否则跳过了也查不出为什么"
    assert fw[0]["weight"] == "light"


def test_立项_auto走调研并把判据写进轨迹(tmp_path, monkeypatch):
    p = _mk_on_disk(tmp_path, monkeypatch, template="product_dev", description="做个平台")
    monkeypatch.setattr(workflow, "run_phase", lambda proj, agents: "stub")

    workflow.start_project_workflow(p, {})

    assert p.phase == Phase.RESEARCHING
    fw = [e for e in p.lineage if e.get("action") == "flow_weight"]
    assert len(fw) == 1
    assert fw[0]["source"] == "auto" and fw[0]["research"] is True


# ── ⑥ 建议器：只建议，不写状态 ───────────────────────────────

def test_建议器_短描述无触发词才建议():
    d = suggest_flow(_mk(template="feature", description="写个转 pdf 的小工具"))
    assert d is not None and d.weight == "light"


def test_建议器_不越权():
    assert suggest_flow(_mk(flow_weight="light", description="小工具")) is None, "已经选过了别多嘴"
    assert suggest_flow(_mk(template="feature", description="x" * 200)) is None, "描述长 → 不嘴"
    assert suggest_flow(_mk(template="feature", description="先定架构方案")) is None, "提到架构了"
    assert suggest_flow(_mk(template="feature", description="")) is None


def test_建议器_不写任何状态():
    p = _mk(template="feature", description="写个转 pdf 的小工具")
    before = p.flow_weight
    suggest_flow(p)
    assert p.flow_weight == before, "建议器必须是纯的，落状态只能靠人点"


# ── ⑦ 模板名别名（三套模板表对不上那条）────────────────────────

def test_模板名别名_前端发的_bugfix_收敛到_bug_fix():
    """前端下拉一直发 `bugfix`，而后端逻辑认的是 `bug_fix` —— 对不上就跑不进模板分支。"""
    from singularity.scheduler.project import normalize_template
    assert normalize_template("bugfix") == "bug_fix"
    assert normalize_template("bug_fix") == "bug_fix"
    assert normalize_template(" feature ") == "feature"

    # 关键后果：别名收敛之后，bug_fix 才吃得到"免调研"这条模板分支
    p = _mk(template=normalize_template("bugfix"))
    assert resolve_flow(p).research is False, "bug_fix → 免调研"


def test_模板名别名_未知值原样放行():
    """别名表只做收敛，**不负责拒收** —— 拒收是 API 层的事，这里不假装。"""
    from singularity.scheduler.project import normalize_template
    assert normalize_template("xxx") == "xxx"
    assert normalize_template("") == ""


# 前端 `Projects.tsx` 那个 `template` 下拉里实际给的全部选项。
# 改前端下拉时**这里也要跟着改** —— 它是"三套表别再漂移"的哨兵。
_FRONTEND_TEMPLATE_OPTIONS = ("product_dev", "agent_dev", "feature",
                              "bug_fix", "refactor", "test", "review")


def test_模板校验集合从_TEMPLATES_派生():
    """**防漂移**：这三套表曾经对不上（后端 4 / API 收 8 / 前端给 5），
    而前端发的 `bugfix` 跟后端逻辑认的 `bug_fix` 压根不是一个名字。

    API 的校验集合现在从 `TEMPLATES` 派生，所以"加了模板忘了补校验"结构上不可能。
    """
    from singularity.scheduler.project import valid_templates, TEMPLATES, _TEMPLATE_ALIASES
    v = valid_templates()
    assert set(TEMPLATES) <= v, "TEMPLATES 里定义了的，API 必须都收"
    assert set(_TEMPLATE_ALIASES) <= v, "别名也要收 —— 旧前端 bundle 还在发 bugfix"

    # 前端下拉提供的值：后端必须都收，否则界面上建项目直接 400
    for t in _FRONTEND_TEMPLATE_OPTIONS:
        assert t in v, f"前端还提供着 {t}，后端不收的话 UI 就建不了"


def test_前端下拉的模板都有定义():
    """前端能选的，`TEMPLATES` 里得有定义 —— 否则 CLI 建不了、表单字段也查不到。"""
    from singularity.scheduler.project import TEMPLATES
    for t in _FRONTEND_TEMPLATE_OPTIONS:
        assert t in TEMPLATES, f"{t} 在前端可选，但 TEMPLATES 里没有定义"
        assert TEMPLATES[t].get("fields"), f"{t} 没有 fields"
