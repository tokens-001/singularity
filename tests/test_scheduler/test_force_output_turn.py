"""「强制输出」那一轮必须**早于最后一轮**，否则模型永远没机会答话。

执行器的工具循环是 `for turn in range(1, max_turns + 1)`。到了强制输出那一步会
撤掉工具、注入一条"现在必须直接输出最终答案"的系统消息，然后 `continue` ——
**若那正好是最后一轮，循环当场结束，那次模型调用从未发生**，raw_output 只剩占位串
`"(达到最大工具轮次, 已产出文件)"`。

2026-09-11 探路轮实测：`max_turns=5` / `max_tool_turns=3`，原判据 `3+2=5`
**恰好等于 max_turns** → 任务文件全写出来了（wc_lite.py + 测试），
交付报告里却一个字总结都没有。
"""
import pytest

from singularity.scheduler.executors.openai_agent import force_output_at


def test_reserves_a_final_turn():
    """判据必须 ≤ max_turns - 1 —— 留至少一轮把终答说出来。"""
    assert force_output_at(5, 3) == 4, "5 轮时必须在第 4 轮就撤工具，才留得下第 5 轮"


def test_real_config_case_is_fixed():
    """复现探路轮那组真实配置（max_turns=5 / max_tool_turns=3）。

    旧判据给 5，正好撞上循环边界 —— 这条就是那次"活干完却只有占位串"的根因。
    """
    assert force_output_at(5, 3) < 5


def test_normal_config_unchanged():
    """默认配置（max_turns=15 / max_tool_turns=3）行为不变 —— 本来就是 5。"""
    assert force_output_at(15, 3) == 5


def test_never_zero_or_negative():
    """max_turns 很小时不许算出 0 或负数（那样一次工具都不让调）。"""
    for mt in (1, 2, 3, 4):
        assert force_output_at(mt, 3) >= 2, f"max_turns={mt} 算出了不合理的阈值"


def test_always_leaves_room():
    """一般不变式：只要 max_turns 够，阈值就不许顶到最后一轮。"""
    for mt in range(3, 21):
        for mtt in range(1, 6):
            if mt - 1 >= 2:
                assert force_output_at(mt, mtt) <= mt - 1, (mt, mtt)
