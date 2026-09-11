"""「一次多动作」实验的度量口径。

执行器**本来就**会把一轮响应里的所有 function_call 全部执行
（`openai_agent.py` 的 `for tc in tool_calls`），卡的不是代码，是**模型不这么吐** ——
SYSTEM_PROMPT 写的是"读→写→测→修"的串行走法，等于在暗示一轮一个动作。

要试就得能量。"省不省轮"以前**没有数据可算**：`tool_events` 只在内存/SSE 里过一遍，
**从来不落盘**（2026-09-12 核过：整个 `.qidian/` grep 不到 `tool:start`）。
`batch_summary` 把每轮调用数摘进 trace，这里钉它的口径。
"""
from singularity.scheduler.neijinglu import batch_summary


def _ev(turn, kind="tool:start", tool="read_file"):
    return {"kind": kind, "turn": turn, "tool": tool}


def test_数轮数和每轮几个():
    evs = [_ev(1), _ev(1), _ev(1),          # 第 1 轮 3 个（一次多动作）
           _ev(2),                            # 第 2 轮 1 个
           _ev(3), _ev(3)]                    # 第 3 轮 2 个
    s = batch_summary(evs)
    assert s["turns"] == 3
    assert s["per_turn"] == [3, 1, 2]
    assert s["total_calls"] == 6
    assert s["batched_turns"] == 2, "只有第 1、3 轮是真·多动作"


def test_只数_start_不数_done():
    """done 事件跟 start 成对出现，都数就把每轮翻倍了。"""
    evs = [_ev(1), _ev(1, kind="tool:done"), _ev(2)]
    assert batch_summary(evs)["per_turn"] == [1, 1], "done 不该被算进去"


def test_旧事件没有_turn_字段就跳过():
    """没有这个字段的老事件**跳过，不猜** —— 猜出来的轮数是假的。"""
    evs = [{"kind": "tool:start", "tool": "read_file"}, _ev(1)]
    s = batch_summary(evs)
    assert s["turns"] == 1 and s["total_calls"] == 1


def test_空输入不炸():
    assert batch_summary([]) == {"turns": 0, "per_turn": [], "total_calls": 0, "batched_turns": 0}
    assert batch_summary(None)["turns"] == 0


def test_一轮一个的串行形态():
    """对照组：全是单动作时 batched_turns = 0 —— 这就是实验要对比的基线形态。"""
    s = batch_summary([_ev(i) for i in range(1, 8)])
    assert s["turns"] == 7 and s["batched_turns"] == 0
