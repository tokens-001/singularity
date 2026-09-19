"""「无文件改动」的**归因**（2026-09-19 复核判据错位审计 A4，用户拍板走"只标归因"）。

判据链的**最后一跳**原来没接上：这事产生在 `_stream_call`（240s 硬顶 / 预算掐断）、
`ExecutorResult` 上也带了 `error_kind` / `truncated_by`，**但 `supervise()` 拿不到** ——
它只看得到 `changed_files=0`，于是无论"它偷懒"还是"**是我们掐的**"，都只写一句
`无文件改动`。

🔴 **`passed` 一分没动**：零产出就是零产出，判通过是假的。改的只是那句**说法**。

变异验证：
  · 把 `our_side_stop` 那支删掉 → 第 1 条红；
  · 让 `our_side_stop_of` 也认 `"exec"` → 第 3 条红（那就变成"什么都赖系统"）。
"""
from singularity.scheduler import supervisor as sup
from singularity.scheduler.executors.base import ExecutorResult


def test_我方掐断时_仍然判fail_但归因说对():
    r = sup._check_completeness(["验收标准"], "输出", [],
                                "任务描述", None, our_side_stop="deadline")

    assert r.passed is False, "放松了 —— 零产出就是零产出"
    assert "我方掐断" in r.reason, r.reason
    assert r.evidence.get("our_side_stop") == "deadline"


def test_不是我方掐断时_照旧是那句无文件改动():
    """**对照**：该有产出却空手回来的，一个字都不该变。"""
    r = sup._check_completeness(["验收标准"], "输出", [], "任务描述")

    assert r.passed is False
    assert r.reason == "无文件改动", r.reason
    assert "our_side_stop" not in r.evidence


def test_只认我方那两档_模型自己失败不算():
    """`exec` 是模型/调用真的失败了，**赖不到我们头上** —— 认它会从
    一个归因错换到另一个（"什么都赖系统"）。"""
    assert sup.our_side_stop_of(
        ExecutorResult(success=False, error_kind="deadline")) == "deadline"
    assert sup.our_side_stop_of(
        ExecutorResult(success=True, truncated_by="max_turns")) == "max_turns"
    assert sup.our_side_stop_of(
        ExecutorResult(success=False, error_kind="exec")) == ""
    assert sup.our_side_stop_of(None) == ""
