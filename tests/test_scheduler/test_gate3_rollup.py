"""观察者的 GATE3 验收汇总 —— 从"纯装饰"接到"真驱动路由"。

背景（2026-09-12 核出来的）：`OBSERVER_VERDICT_SCHEMA` 全仓只有一处用途 ——
`json.dumps` 成字符串**贴进 prompt**；`overall` / `fix_route_decision`
**没有任何代码读**，而且注入只发生在"没有 project_id"的旧兼容路径上。
即：那份 schema 在真实用法里从来没被要求过，要求了也没人接。

现在接上了，所以这里钉三件事：
1. 解析**必须校验枚举**（fail-closed）—— 这个值决定项目回退到哪一层
2. 落盘位置读写两侧**必须同一条路径**
3. 没有汇总时，`handle_gate3_reject` 的行为**跟改动前逐字一致**
"""
import json
import pytest

from singularity.scheduler import config
from singularity.scheduler import project as proj_mod
from singularity.scheduler import workflow
from singularity.scheduler.project import Phase, ProjectState
from singularity.scheduler._observer_definition import (
    parse_verdict_rollup, VERDICT_FIX_ROUTES, VERDICT_OVERALLS,
)


# ── ① 解析 + 校验 ──────────────────────────────────────────

class TestParseRollup:
    def test_正常(self):
        r = parse_verdict_rollup('{"qa_summary":"x","fix_route_decision":"impl","overall":"no_go"}')
        assert r["fix_route_decision"] == "impl" and r["overall"] == "no_go"

    def test_夹在自由文本里也能抠出来(self):
        r = parse_verdict_rollup('先说一段话 {"fix_route_decision":"note","overall":"go"} 后面还有')
        assert r["fix_route_decision"] == "note"

    @pytest.mark.parametrize("bad", [
        '{"fix_route_decision":"IMPL","overall":"no_go"}',      # 大小写
        '{"fix_route_decision":"不通过","overall":"no_go"}',     # 中文
        '{"fix_route_decision":"impl"}',                        # 缺 overall
        '{"overall":"no_go"}',                                  # 缺 route
        '{"fix_route_decision":"impl","overall":"maybe"}',       # overall 不在枚举里
        '完全不是 JSON', '', '[]', 'null',
    ])
    def test_不合法一律_None(self, bad):
        """**fail-closed**：宁可退回原逻辑，也不能猜一个回退层级出来。"""
        assert parse_verdict_rollup(bad) is None

    def test_枚举是这几个值(self):
        assert VERDICT_FIX_ROUTES == {"impl", "design", "note"}
        assert VERDICT_OVERALLS == {"go", "no_go", "needs_human"}


# ── ② 读写同一条路径 ────────────────────────────────────────

@pytest.fixture
def tmp_qidian(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path / "qidian")
    (tmp_path / "qidian").mkdir(exist_ok=True)
    monkeypatch.setattr(proj_mod, "get_projects_root", lambda: tmp_path / "projects")
    return tmp_path


def _write_rollup(pid: str, route: str):
    from singularity.scheduler.project import get_project_dir
    (get_project_dir(pid) / "observer_rollup.json").write_text(
        json.dumps({"fix_route_decision": route, "overall": "no_go"}), encoding="utf-8")


def test_落盘后读得回来(tmp_qidian):
    p = ProjectState(id="p1", name="n")
    proj_mod.save(p)
    _write_rollup("p1", "design")
    assert workflow._read_observer_rollup("p1") == "design"


def test_没有文件返回_None(tmp_qidian):
    p = ProjectState(id="p1", name="n")
    proj_mod.save(p)
    assert workflow._read_observer_rollup("p1") is None


def test_写读闭环(tmp_qidian):
    """⚠️ **这条才是关键**：写入方在 `_observer_answer`，读取方在 `workflow`，
    两边各算各的路径。对不上的话整条链路**静默失效** —— 汇总裁了，没人读，
    而且哪儿都不报错（这个仓库最典型的那种坏法）。
    """
    from singularity.scheduler._observer_answer import _persist_gate3_rollup
    proj_mod.save(ProjectState(id="p1", name="n", phase=Phase.GATE3))

    _persist_gate3_rollup("p1", '小结 {"fix_route_decision":"note","overall":"go"}')

    assert workflow._read_observer_rollup("p1") == "note", "写了但读不到 = 路径不一致"


def test_不在_GATE3_就不写(tmp_qidian):
    """别的阶段聊天不该覆盖 GATE3 的裁决。"""
    from singularity.scheduler._observer_answer import _persist_gate3_rollup
    proj_mod.save(ProjectState(id="p1", name="n", phase=Phase.EXECUTING))

    _persist_gate3_rollup("p1", '{"fix_route_decision":"note","overall":"go"}')

    assert workflow._read_observer_rollup("p1") is None


def test_回复里没有裁决就不写(tmp_qidian):
    """模型没按 schema 回（很常见）→ 什么都不写，别留个半成品文件骗下游。"""
    from singularity.scheduler._observer_answer import _persist_gate3_rollup
    proj_mod.save(ProjectState(id="p1", name="n", phase=Phase.GATE3))

    _persist_gate3_rollup("p1", "我觉得这个项目还行，你自己看着办")

    assert workflow._read_observer_rollup("p1") is None


def test_文件里是非法值也返回_None(tmp_qidian):
    p = ProjectState(id="p1", name="n")
    proj_mod.save(p)
    from singularity.scheduler.project import get_project_dir
    (get_project_dir("p1") / "observer_rollup.json").write_text('{"fix_route_decision":"XXX"}',
                                                               encoding="utf-8")
    assert workflow._read_observer_rollup("p1") is None


# ── ③ 路由行为：有汇总 / 没汇总 ──────────────────────────────

def _gate3_project(pid="p1"):
    return ProjectState(id=pid, name="n", phase=Phase.GATE3)


def test_有汇总时按汇总路由(tmp_qidian):
    """汇总说 design → 回规划、清空架构，且 lineage 记下**来源**（§44 可追溯）。"""
    p = _gate3_project()
    proj_mod.save(p)
    _write_rollup("p1", "design")

    workflow.handle_gate3_reject(p, {}, feedback="人打回")

    assert p.phase == Phase.PLANNING
    assert p.architecture is None
    route = [e for e in p.lineage if e.get("action") == "gate3_route"][-1]
    assert route["route"] == "design" and route["source"] == "observer_rollup"


def test_没有汇总时行为跟改动前一致_无报告回实现层(tmp_qidian):
    """**回归**：没有 observer 汇总时，一个字都不该变 ——
    无 QA 报告 → 默认 impl（不动架构），免得空转 GATE2。"""
    p = _gate3_project()
    proj_mod.save(p)

    workflow.handle_gate3_reject(p, {}, feedback="人打回")

    assert p.phase == Phase.EXECUTING
    route = [e for e in p.lineage if e.get("action") == "gate3_route"][-1]
    assert route["route"] == "impl" and route["source"] == "default_no_qa"
    assert route.get("no_qa") is True


def test_汇总能盖过无报告时的默认(tmp_qidian):
    """观察者真给了裁决，就听它的 —— 即使没有 QA 报告。"""
    p = _gate3_project()
    proj_mod.save(p)
    _write_rollup("p1", "note")

    workflow.handle_gate3_reject(p, {}, feedback="人打回")

    route = [e for e in p.lineage if e.get("action") == "gate3_route"][-1]
    assert route["route"] == "note" and route["source"] == "observer_rollup"
