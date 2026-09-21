"""流程复利账本：把每次交付的**结构化结局**记下来，供下一轮参考。

分析里的 P6。三个乘数里"**复利**"是唯一属于自己那个（模型是租的、流程是行业常识），
而它今天 ≈ 0 —— 系统记了一切（trace / lineage / 告警 / 用量 / 合并结果），
**却几乎不自动提炼任何东西回灌流程**。这是从 0 到 1 的那一步。

## 两条规矩

1. **独立文件**（`process_ledger.json`），**只追加**。
   §39 那条：内部记录别塞进会被"整行重建"的地方 —— 项目文件、模型表都会整行重写。
2. **digest 是给人/给模型看的五行**，不是原始数据倾倒。原始数据在 json 里。

⚠️ **它记的是"上次实际发生了什么"，不是"应该怎么做"**。结论要人来下 ——
账本自己不下判断（那会变成一台自己教自己的回音壁）。
"""
from __future__ import annotations

import time
from pathlib import Path

from singularity.scheduler import config as sched_config

_MAX_ROWS = 500          # 有上限就写出来，别静默截断


def _path() -> Path:
    """**读时现算**，不在模块级缓存（防御模式 #34：冻在构造时的路径会写进生产）。"""
    return sched_config.QIDIAN_DIR / "process_ledger.json"


def _status_str(task) -> str:
    """任务状态 → 字符串。枚举取 `.value`（`str(Enum)` 给的是 `TaskStatus.DONE`，
    不是 `done` —— 拿它直接比会静默不等）。"""
    st = getattr(task, "status", None)
    v = getattr(st, "value", st)
    return str(v or "")


def load() -> list[dict]:
    """读账本。**损坏时返回空表 —— 但那是"带告警的降级"**（隔离 + 双通道出声在 `_io` 里做了）。

    ⚠️ 原来是裸 `json.loads` + `except (FileNotFoundError, JSONDecodeError, OSError): return []`
    —— "文件坏了"和"还没有账本"长得一模一样，**读侧分不出来**（2026-09-14，C 的 S1 草案，我核过）。
    """
    from singularity.scheduler._io import load_json_or_quarantine
    data = load_json_or_quarantine(_path(), expect=list)
    return data if data is not None else []


def record(project, extra: dict | None = None) -> dict:
    """把一次交付的结局追加进账本。返回写进去的那一行。

    **只记账，不下结论。** 各项失败计数（review_failures / integrate_failures）
    与 issues 的**条数**照实记；成本取现有累计口径。
    """
    # 这里**绕过 `load()` 直取三态** —— 它要知道"坏"和"空"的区别（读侧可以降级，
    # 写侧不行）。本文件的铁律是"记账失败不能把交付带崩"，所以**不 raise**，
    # 改成：损坏期间**拒写**（见下面落盘那段），本轮照常返回 row。
    from singularity.scheduler._io import load_json_or_quarantine
    rows = load_json_or_quarantine(_path(), expect=list)
    rows_corrupt = rows is None
    if rows_corrupt:
        rows = []          # 本轮照常算这一行，但**落盘前拒写**（见下）
    ids = list(getattr(project, "task_ids", []) or [])
    done = 0
    try:
        from singularity.scheduler import tracker as _tk
        for t in ids:
            r = _tk.read_task(t)
            if r is None:
                continue
            # ⚠️ `str(TaskStatus.DONE)` 是 `'TaskStatus.DONE'`，不是 `'done'` ——
            # 拿它去 `endswith("done")` 大小写不匹配，**恒为 False**，
            # 于是 tasks_done 永远是 0（我第一版就是这么写错的，真跑才发现）。
            if _status_str(r) == "done":
                done += 1
    except Exception:
        # S2：失败写 0 = **编造"一条都没成"**（缺值有人问，0 没人问）。
        # 同类不同命：紧挨着的 `cost` 早就是 `None` 了。这一格会经 `digest()`
        # 拼进架构 prompt 的"上一轮实际发生了什么" —— 编造的是**事实**。
        done = None
    cost = None
    try:
        from singularity.scheduler._token_budget import _budget
        cost = _budget.project_spend_total(getattr(project, "id", "") or "")
    except Exception:
        pass

    issue_kinds: dict[str, int] = {}
    for i in (getattr(project, "issues", []) or []):
        if isinstance(i, dict):
            k = str(i.get("type", "?"))
            issue_kinds[k] = issue_kinds.get(k, 0) + 1

    row = {
        "ts": time.time(),
        "project_id": getattr(project, "id", "") or "",
        "name": (getattr(project, "name", "") or "")[:60],
        "template": getattr(project, "template", "") or "",
        "phase": getattr(getattr(project, "phase", None), "value", None),
        "tasks_total": len(ids),
        "tasks_done": done,
        "review_failures": getattr(project, "review_failures", 0) or 0,
        "integrate_failures": getattr(project, "integrate_failures", 0) or 0,
        "issues": issue_kinds,
        "cost_usd": cost,
        "budget_usd": getattr(project, "token_budget_total", 0) or 0,
    }
    if extra:
        row.update(extra)

    rows.append(row)
    if len(rows) > _MAX_ROWS:
        rows = rows[-_MAX_ROWS:]
    if rows_corrupt:
        # 🔴 **拒写**：账本坏了、已经隔离到 `.corrupt`，这轮**不许**拿这行去整份重建 ——
        # 重建出来的"账本"只剩今天这一行，历史全没了，而外表和正常账本一模一样。
        # 模块自己的规矩是"宁可表小，不可表假"；这里是它第一次真兑现。
        from singularity.scheduler import witness
        witness.warn("process_ledger",
                     "record_skip: 账本损坏已隔离(.corrupt), 本轮不落账, 拒绝整份重建",
                     key="ledger_corrupt")
        return row
    try:
        # ⚠️ **原子写**，不是裸 `write_text` —— 撕裂一次就把这个文件写坏了，
        # 而上面那段"读坏就拒写"的新语义会让它**从此拒写到重启**：
        # 非原子写把"丢一轮"放大成"停摆"，等于把诱因留在原地、还加重了后果
        # （2026-09-14 外派⑦ 点名这条；`_dispatch_crud`/`model_registry` 早换了）。
        from singularity.scheduler._io import atomic_write_json
        atomic_write_json(_path(), rows, indent=1)   # 人读的账本，一直用 1 空格缩进
    except Exception:
        pass      # 记账失败不能把交付带崩
    return row


def digest(limit: int = 5) -> str:
    """最近几轮的结构化结局 → 几行"上一轮发生了什么"，供注入架构 prompt。

    ⚠️ 措辞是**陈述事实**，不是"教训" —— 系统没资格替人总结该怎么做。
    """
    rows = load()[-limit:]
    if not rows:
        return ""
    lines = []
    for r in rows:
        # ⚠️ `tasks_done` 可能是 `None`（那一轮数不出来，见上面 S2 那处）。
        # **不能拿 `or 0` 折算** —— 那会把"不知道"重新写成"一条都没成"，
        # 正是上面刚拆掉的那个编造，只是换了个地方编。未知就印 `?`、也不报"失败 N"。
        _done = r.get("tasks_done")
        _total = r.get("tasks_total", 0) or 0
        bits = [f"任务 {_done if _done is not None else '?'}/{_total} 成功"]
        if _done is not None and _total - _done > 0:
            bits.append(f"失败 {_total - _done}")
        # ⚠️ 这里原来还有一句 `if r.get("fix_round"): "返工 N 轮"` —— **永不触发**
        #    （`project.fix_round` 全仓没有 `+= 1`，恒为 0）。2026-09-19 连同字段一起删了。
        #    真要回答"返工了几轮"，现成的是 `review_failures` / `integrate_failures`
        #    这两条已经记着的；**任务内的轮次重试**（`_exec._decide_cascade` 的
        #    `("continue", feedback)`）仍然只活在内存里，没落盘。
        if r.get("integrate_failures"):
            bits.append(f"集成失败 {r['integrate_failures']} 次")
        top = sorted((r.get("issues") or {}).items(), key=lambda kv: -kv[1])[:2]
        if top:
            bits.append("主要问题: " + "、".join(f"{k}×{v}" for k, v in top))
        if r.get("cost_usd") is not None:
            bits.append(f"花费 ${r['cost_usd']}")
        lines.append(f"- {str(r.get('name', ''))[:30]}（{r.get('phase')}）: " + "；".join(bits))
    return "\n".join(lines)
