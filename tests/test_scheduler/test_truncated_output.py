"""「**成功但其实被截断**」要有结构化出口（2026-09-19 复核判据错位审计 A3）。

`openai_agent` 达到最大工具轮次**且有文件改动**时返回
`success=True, raw_output="(达到最大工具轮次, 已产出文件)"`。
**那个判定是对的**（任务的价值是产物文件；判失败会白烧一轮重试、还会把账丢掉）——
本文件**不动它**。

问题是**"被截断"这件事只活在正文里**：`success=True` / `error=""` / `error_kind=""`
三个字段读起来都是"正常做完"，读得到那句占位串的是**人**、不是判据。

补法：`ExecutorResult.truncated_by`（与 `error_kind` **分开** —— 那档说"为什么失败"，
这一档**成功时也会出现**）+ 交付报告里的 `agent_output_truncated_by`。

变异验证（各能掐红）：
  · `to_dict` 里去掉那个键 → 前两条红；
  · `from_dict` 重建时不捡 `truncated_by` → 第 2 条红（报告重建一次就丢标记）；
  · 把 `_dispatch_exec` 的成功判据"收紧"成 `result.success` → 第 3 条红。
"""
from singularity.scheduler.neijinglu import DeliveryReport


def test_正常完成时不带标记():
    """**对照**：真正的终答不该被标成截断 —— 否则这个字段就成了新的噪声。"""
    out = DeliveryReport.from_dict({"agent_output": "这里是真正的终答"}).to_dict()
    assert not out["agent_output_truncated_by"], out["agent_output_truncated_by"]


def test_报告带出被截断标记_重建路也不丢():
    """`from_dict` 是**第二条路**（`GET /api/tasks/<id>/trace` 走它）。
    `to_dict` 写了、重建时不捡回来 ⇒ **报告重建一次标记就没了**（§60 的形状）。"""
    d = {"agent_output": "(达到最大工具轮次, 已产出文件)",
         "agent_output_truncated_by": "max_turns"}

    out = DeliveryReport.from_dict(d).to_dict()

    assert out["agent_output_truncated_by"] == "max_turns", (
        "被截断这件事又只能从正文里读了")


def test_成功但零产出_仍然要换模型重试(monkeypatch):
    """**A8 的不变量 —— 别把这条判据"收紧"成 `result.success`。**

    `claude_cli` 那条是 `success=True, raw_output=proc.stdout`，**stdout 可能是空的**：
      · 现在的判据（`raw_output` 非空）：空 ⇒ 判失败 ⇒ 换模型重试 —— **严的一侧**；
      · 换成 `result.success`：判成功 ⇒ 直接 return，**交回一份"成功但零产出"**。
    ⇒ 那是**放松**门，不是收紧。
    """
    import pytest

    from singularity.scheduler import _dispatch_exec as pd
    from singularity.scheduler.executors.base import ExecutorResult

    # 一次就成？不 —— 桩说"成功但空产出"，它必须继续走 fallback（最终全失败）
    monkeypatch.setattr(pd, "pick_agent_fallback_chain",
                        lambda *a, **k: [{"model": "m", "type": "openai-agent"}])
    monkeypatch.setattr(pd, "_prefer_by_strengths", lambda task, chain: chain)
    monkeypatch.setattr(pd, "_committee_allowed", lambda *a, **k: False)
    monkeypatch.setattr(pd, "_ensure_agent_type", lambda c: c)
    monkeypatch.setattr(pd, "_model_breaker",
                        type("B", (), {"record_failure": lambda *a: None,
                                       "record_success": lambda *a: None})())
    monkeypatch.setattr(pd, "_run_executor",
                        lambda *a, **k: ExecutorResult(success=True, raw_output="",
                                                       error="", error_kind=""))

    with pytest.raises(RuntimeError):
        pd.dispatch("任务", "any", "tid", {})
