"""**列表工具的输出要有上界** —— 它直接进 LLM 的 prompt（2026-09-27 真机）。

## 症状
观察者一次"门汇报"吃了 **108,968 token** —— 那一轮总共 205,603，它一个人占 53%，
**比项目三个任务 + 架构 + QA + 安全审计（6 次调用合计 90,243）还贵**。

## 根因
`_tool_list_tasks` 把每个任务的**整份 dict** 原样返回，而本仓 17 个任务的
**`description` 一项就占 64,965 / 68,723 字符（94.5%）**（描述里塞着验收标准 +
约束 + 机器检查命令，单条最长 4,286 字符）。而 `_answer_question_inner` 是**工具循环**
（`max_turns` 默认 3），**每轮把累积的消息重发** ⇒ 一次调用就十万。

⚠️ **"那次调的就是它"是按体积推断的**（`list_tasks` 是唯一返回 76KB 的工具；
观察者的工具调用**没有日志**，我证明不了）—— 但"列表不该返回全文"本身站得住。
**全文出口一直有**：`get_task_details(task_id)`。

⇒ 老形状又一例：**一个只给人和模型看的"列表"，返回了一个没有上界的载荷。**
"""

import json

import pytest

from singularity.scheduler import _observer_tools as OT
from singularity.scheduler import config, tracker


@pytest.fixture
def _iso(monkeypatch, tmp_path):
    """把 `.qidian` 指到 tmp，任务文件写在它下面的 tasks/。抄 `test_observer_worker`。"""
    monkeypatch.setattr(config, "QIDIAN_DIR", tmp_path)
    (tmp_path / "tasks").mkdir(parents=True, exist_ok=True)

    def _mk(task_id: str, desc: str, status: str = "running"):
        (tmp_path / "tasks" / f"{task_id}.json").write_text(json.dumps({
            "id": task_id, "description": desc, "status": status,
            "created_at": 1, "updated_at": 2}), encoding="utf-8")

    return _mk


def test_超长描述被截断(_iso):
    """判据：**截断 + 带省略号**（省略号是给模型的信号：后面还有）。

    变异：把 `_tool_list_tasks` 里那段截断删掉 ⇒ 本条红。
    """
    _iso("big", "长" * (OT._LIST_DESC_CHARS + 500))

    got = OT._tool_list_tasks()
    desc = got[0]["description"]

    assert len(desc) <= OT._LIST_DESC_CHARS + 1, f"没截断：{len(desc)} 字符"
    assert desc.endswith("…"), f"截了但没告诉人后面还有：{desc[-12:]!r}"


def test_短描述原样不动(_iso):
    """反方向对照：**别把正常任务也改坏** —— 不截断就不该加省略号。"""
    _iso("small", "就一句话")

    assert OT._tool_list_tasks()[0]["description"] == "就一句话"


def test_整份返回有上界(_iso):
    """钉住真正的不变量：**返回值的大小跟着"有几条"走，不跟着"每条多长"走**。

    这条才是当初烧钱的那个量 —— 上面两条只钉了单条。造 30 条超长描述，
    断言描述合计不超过 `上限 × 条数`。
    """
    n = 30
    for i in range(n):
        _iso(f"t{i:03d}", "很长" * 2000)

    got = OT._tool_list_tasks()
    total = sum(len(t.get("description") or "") for t in got)

    assert len(got) == n, f"夹具没生效（应 {n} 条）：{len(got)}"
    assert total <= (OT._LIST_DESC_CHARS + 1) * n, (
        f"描述合计 {total} 字符，上界是 {(OT._LIST_DESC_CHARS + 1) * n} —— 又没上界了")
