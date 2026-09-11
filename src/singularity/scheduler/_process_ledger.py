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

import json
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
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def record(project, extra: dict | None = None) -> dict:
    """把一次交付的结局追加进账本。返回写进去的那一行。

    **只记账，不下结论。** 各项失败计数（fix_round / review_failures /
    integrate_failures）与 issues 的**条数**照实记；成本取现有累计口径。
    """
    rows = load()
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
        done = 0
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
        "fix_round": getattr(project, "fix_round", 0) or 0,
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
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                           encoding="utf-8")
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
        fail = (r.get("tasks_total", 0) or 0) - (r.get("tasks_done", 0) or 0)
        bits = [f"任务 {r.get('tasks_done', 0)}/{r.get('tasks_total', 0)} 成功"]
        if fail > 0:
            bits.append(f"失败 {fail}")
        if r.get("fix_round"):
            bits.append(f"返工 {r['fix_round']} 轮")
        if r.get("integrate_failures"):
            bits.append(f"集成失败 {r['integrate_failures']} 次")
        top = sorted((r.get("issues") or {}).items(), key=lambda kv: -kv[1])[:2]
        if top:
            bits.append("主要问题: " + "、".join(f"{k}×{v}" for k, v in top))
        if r.get("cost_usd") is not None:
            bits.append(f"花费 ${r['cost_usd']}")
        lines.append(f"- {str(r.get('name', ''))[:30]}（{r.get('phase')}）: " + "；".join(bits))
    return "\n".join(lines)
