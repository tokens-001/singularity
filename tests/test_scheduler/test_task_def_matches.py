"""架构任务认领：`T1` 不许命中 `[T11]`（2026-09-25）。

Qoder 外派审查 #7（`docs/Qoder-审查-20260924.md`），我核过。

判据原来是**裸子串** `tdef["id"] in task.description`，而描述里写的是
`f"[{local_id}] {desc}"` ⇒ `"T1" in "[T11] 输出模块…"` **为真**
⇒ 12 个任务的项目里（c 轮 12 个、09-17 那轮 11 个，是常态）**T11 认领到 T1 的验收标准**。

两处同一形状，下游各喂一把尺子 —— 量到的都是别人的东西：
  · `supervisor.qa_context` → `checklist` → `_check_laziness` 拿 `len(checklist)` 当软尺子；
  · `_exec._declared_files_for` → `_model_discipline.record_scope` 的"声明范围"。
    而且这条是"**取第一个匹配就返回**" ⇒ 架构里 T1 排在前面时，T11 拿到的是 T1 的文件表。

反向也成立：任一 tdef 缺 `id` / 缺 `title` ⇒ `"" in desc` **恒真** ⇒ 它认领**每一条**任务。
"""
from types import SimpleNamespace as NS

from singularity.scheduler import _exec as X
from singularity.scheduler import config, project as proj_mod
from singularity.scheduler import supervisor as sup

import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """这个文件会**真建项目**（写 `.qidian/projects/`），必须指到临时目录。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)


def _tdef(tid, title="", acceptance="", files=None):
    d = {"id": tid, "title": title, "acceptance": acceptance}
    if files is not None:
        d["estimated_files"] = files
    return d


# ═══════════════════════════════════════════════════════════════
# ① 判据本身
# ═══════════════════════════════════════════════════════════════

def test_裸子串会命中_T1_在_T11_里():
    """**这条钉的就是那个 bug** —— 它是判据的负例，必须为假。"""
    desc = "[T11] 输出模块 render.py\n验收标准: ..."
    assert not sup.task_def_matches(_tdef("T1"), desc), \
        "T1 认领了 T11 的描述 —— 12 个任务的项目里，T11 会拿到 T1 的验收标准和文件表"


def test_方括号标记正常命中():
    desc = "[T11] 输出模块 render.py\n验收标准: ..."
    assert sup.task_def_matches(_tdef("T11"), desc)
    assert sup.task_def_matches(_tdef("T1"), "[T1] 解析模块\n验收标准: ...")


def test_两位数和一位数都不串味():
    """T1/T2 … T12 全排一遍 —— 每条描述**只**被它自己那条定义认领。"""
    descs = {i: f"[T{i}] 第 {i} 个模块\n验收标准: ..." for i in range(1, 13)}
    for i, desc in descs.items():
        hits = [j for j in descs if sup.task_def_matches(_tdef(f"T{j}"), desc)]
        assert hits == [i], f"T{i} 的描述被 T{hits} 认领了"


def test_缺id或缺口title的定义不许认领所有任务():
    """`"" in desc` 恒真 —— 任一 tdef 少一个字段就会认领**每一条**任务。

    变异：去掉 `task_def_matches` 里那两个非空判断 ⇒ 这条红。
    """
    desc = "[T3] 某个模块\n验收标准: ..."
    assert not sup.task_def_matches({"acceptance": "x"}, desc), "没有 id、没有 title 也认领了"
    assert not sup.task_def_matches({"id": "", "title": ""}, desc)


def test_title匹配照旧生效():
    """**对照**：架构师给的散文标题没法加方括号，所以 title 逐字匹配要留着。

    ⚠️ 描述里**不放**方括号标记 —— 放了的话 id 那条先命中，这条就测不到 title 那半了。
    """
    desc = "实现日志解析模块\n验收标准: ..."
    assert sup.task_def_matches(_tdef("", title="实现日志解析模块"), desc)
    assert not sup.task_def_matches(_tdef("", title="另一个模块"), desc)


# ═══════════════════════════════════════════════════════════════
# ② 接线：两条消费端拿到的必须是**本任务**的那份
# ═══════════════════════════════════════════════════════════════

def _project_with_two_tasks():
    """T1 排在前面 —— 正是"取第一个匹配就返回"会踩的顺序。"""
    p = proj_mod.create("认领测试")
    p.architecture = {"tasks": [
        _tdef("T1", title="解析模块", acceptance="T1 的验收标准", files=["parser.py"]),
        _tdef("T11", title="输出模块", acceptance="T11 的验收标准", files=["render.py"]),
    ]}
    proj_mod.save(p)
    return p


def test_接线_qa_context拿的是本任务的验收标准():
    """变异：把 `task_def_matches` 里的 `f"[{tid}]"` 改回裸 `tid` ⇒ 这条红。"""
    p = _project_with_two_tasks()
    task = NS(project_id=p.id, description="[T11] 输出模块 render.py\n验收标准: ...")

    _, checklist = sup.qa_context(task)

    assert checklist == ["T11 的验收标准"], \
        f"T11 拿到的是别人的验收标准：{checklist} —— 它会拿 T1 的要求去量 T11 的产出"


def test_接线_声明文件表拿的是本任务的那份():
    """`_declared_files_for` 是"取第一个匹配就返回"，所以顺序也钉一下。"""
    p = _project_with_two_tasks()
    task = NS(project_id=p.id, description="[T11] 输出模块 render.py\n验收标准: ...")

    assert X._declared_files_for(task) == ["render.py"], \
        "T11 拿到的是 T1 的文件表 —— `record_scope` 之后会拿别人的范围量它"
