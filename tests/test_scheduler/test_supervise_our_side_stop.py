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


def test_被我们掐断时_软信号不亮_判词不再自相矛盾():
    """🔴 真机现场（`round-20260921c` T1）：同一份判词里
    「是被我方掐断的，**不是**空手回来的偷懒」**紧跟**「检测到 1 个偷懒信号」。

    根因：两条软信号**都是拿 `changed_files` 当尺子** —— 被掐断 ⇒ 最后一次调用零产出
    ⇒ 必然 0 文件 ⇒ 必然亮。形状和 `[只读]` 那条**一模一样**。
    """
    r = sup._check_laziness("", [], ["实现 X 模块", "写测试"], "写 X 模块",
                            our_side_stop="deadline")

    assert r.passed is True, r.reason
    # ⚠️ 别用 `"偷懒信号" not in reason` —— 「无偷懒信号」里也有这四个字（判据没判别力）。
    assert r.reason == "无偷懒信号", r.reason


def test_对照组_没被掐断时空手回来照旧判():
    """**这条是对照**：不是把软信号整个关掉 —— 真偷懒的照旧判。"""
    r = sup._check_laziness("", [], ["实现 X 模块", "写测试"], "写 X 模块")

    assert r.passed is False
    assert len(r.evidence["signals"]) == 2, r.evidence


def test_被掐断不等于免检_硬信号照常生效():
    """掐断只是让"零改动"这条尺子失效，**输出本身糊弄照旧算**（同 `[只读]`）。"""
    r = sup._check_laziness("# TODO: 实现\n", [], ["实现 X 模块"], "写 X 模块",
                            our_side_stop="deadline")

    assert r.passed is False
    assert r.evidence.get("hard") is True, r.evidence


def test_接线_supervise真的把_our_side_stop_传给了偷懒检查():
    """**这条钉接线**：删掉 `supervise()` 里 `_check_laziness(...)` 的那个实参，
    它又看不见"是我们掐的" ⇒ 那份自相矛盾的判词原样回来。

    只测 `_check_laziness` 自己验的是"函数对"，验不到"接线通"（同族栽过多次）。
    """
    v = sup.supervise(
        task_description="写 X 模块",
        changed_files=[],
        constraints=[],
        checklist=["实现 X 模块", "写测试"],
        agent_output="",
        our_side_stop="deadline",
    )

    assert any("completeness" in i for i in v.issues), v.issues
    assert not any("laziness" in i for i in v.issues), v.issues


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
