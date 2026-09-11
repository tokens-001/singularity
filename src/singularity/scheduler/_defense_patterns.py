"""把「防御模式」从教材变成**审查员的找茬清单**。

## 为什么是这个形态（而不是原方案）

分析里的 P5 提议"按任务类型匹配 2-3 条注入 prompt，知识从人读变成系统吃"。
逐条过了一遍那 58 条：**绝大多数是写给"改平台代码的人"的** ——
`#8 SSE 断线重连不清理队列`、`#13 跨线程改共享状态没守锁约定`、
`#53 记账的调用点等于覆盖面`…… **跑在流水线里的模型读这些没用，它不写平台代码。**
（分析自己举的例子也站不住：#6 的错在 `_exec.py` 没传 `cwd`；#3/#25 说的是平台
审查层的代码写错，不是"审查员该去找什么"。）

**唯一站得住的用法**：这些条目的「**症状**」本来就是一行行的**失败长相** ——
把它们当**检查表**交给审查员：不是"你别这么干"，而是"**看看有没有长这样**"。

⚠️ **有没有用没验证。** 这是一个方向合理但没测过的赌注；所以：
· 硬性限条数（默认 3）—— prompt 膨胀是它自己提的"唯一的险"
· 挑不中就**什么都不加**，不硬凑

（同族的赌注也记在这儿：`_review` 那边选审查员、`fusion` 那边选定稿人，
都是"用历史挑更靠谱的那个"—— 但那些是**有数据支撑**的，这条没有。）
"""
from __future__ import annotations

import re
from pathlib import Path

from singularity.scheduler import config as sched_config

MAX_ITEMS = 3            # 硬上限；理由见模块头
# 命中门槛（余弦）。实测拿 5 个场景量过：**真命中 0.185~0.448，误报 0.052~0.064**
# —— 中间是空的，0.12 能把两边干净切开。低于它就**什么都不给**（宁可不给，别硬凑）。
_MIN_COS = 0.12
_MIN_OVERLAP = 3         # 再兜一道：光靠一两个 bigram 撞上不算数


def _doc_path() -> Path:
    """防御模式文档的位置。**读时现算**（§34）。

    它就在奇点仓库里 —— 单一事实源。**不复制一份进代码**：本项目
    反复吃过"文档和代码各存一份、然后悄悄漂移"的亏（见 `演化史` 里那些）。
    """
    return sched_config.PROJECT_ROOT / "docs" / "防御模式.md"


def load_patterns() -> list[dict]:
    """把文档解析成 [{id, title, symptom, rule}]。解析不了 → []。

    格式约定（`### N. 标题` + `- **症状**：...` + `- **规则**：...`）。
    格式变了就返回空 —— **不猜**，也不半解析出些错条目。
    """
    try:
        text = _doc_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []

    out: list[dict] = []
    blocks = re.split(r"(?m)^###\s+", text)
    for b in blocks[1:]:
        head, _, body = b.partition("\n")
        m = re.match(r"([0-9]+[a-z]?)\.\s*(.+)", head.strip())
        if not m:
            continue
        pid, title = m.group(1), m.group(2).strip()
        sym = re.search(r"-\s*\*\*症状\*\*[：:]\s*(.+?)(?:\n-\s*\*\*|\n###|\Z)",
                        body, re.S)
        if not sym:
            continue
        # 症状可能跨行（续行缩进），压成一行
        symptom = " ".join(sym.group(1).split())
        out.append({"id": pid, "title": title, "symptom": symptom[:200]})
    return out


def _bigrams(s: str) -> set[str]:
    """字符二元组 —— 中文没法按空格分词，这个不用引依赖、够用。"""
    t = re.sub(r"[\s\W_]+", "", s or "")
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else set()


def pick_for(context: str, n: int = MAX_ITEMS) -> list[dict]:
    """按重合度挑最相关的几条。挑不中就返回 []（**宁可不给，别硬凑**）。

    context 一般给：任务描述 + 改动的文件名 + （可选）diff 摘要。
    """
    ctx = _bigrams(context)
    if not ctx:
        return []
    scored = []
    for p in load_patterns():
        hay = _bigrams(f"{p['title']} {p['symptom']}")
        if not hay:
            continue
        overlap = len(ctx & hay)
        # **按余弦排，不按重合条数** —— 按绝对条数的话，长条目天然占便宜
        # （bigram 多 = 撞上的机会多），实测会挑出"55 超时"这种跟上下文无关的。
        score = overlap / ((len(ctx) * len(hay)) ** 0.5)
        if overlap >= _MIN_OVERLAP and score >= _MIN_COS:
            scored.append((score, p))
    scored.sort(key=lambda kv: (-kv[0], kv[1]["id"]))
    return [p for _, p in scored[:max(1, min(n, MAX_ITEMS))]]


def checklist(context: str, n: int = MAX_ITEMS) -> str:
    """给审查员的**找茬清单**（几行）。挑不中 → ""（不加，别占预算）。"""
    items = pick_for(context, n)
    if not items:
        return ""
    lines = [f"- [{p['id']}] {p['symptom']}" for p in items]
    return ("【历史上出过的这类毛病（**挑毛病时留意有没有长这样**，"
            "没有就忽略，别硬套）】\n" + "\n".join(lines))
