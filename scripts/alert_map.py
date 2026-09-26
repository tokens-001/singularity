#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 `.qidian/alerts.jsonl` 里的告警 key 映射回「发出它的源码文件」。

两个用途：
  ① 冷启动按症状找入口 —— `CLAUDE.md`「改 bug 先看哪」那张表就是它生成的；
  ② 让那张表**不会烂**：跑一遍就知道此刻盘上有哪些 key、各是谁发的。
     （反例：`scheduler/ARCHITECTURE.md` 09-26 刚被删掉 —— 它写死了行数、
     没有重新生成的路子，于是漂了三个月还顶着"架构约束文件"的标题。）

为什么需要"前缀回退"：key 常常是 f-string 拼出来的，**字面量在源码里根本不存在**。
实测 `xml_tool_calls_recovered`（80 条）在 `src/` 里 grep 全串是 0 命中，
真正的发出点是 `openai_agent.py` 那句 `f"xml_tool_calls_{_scope}:"` ⇒ 按 `_` 逐段
截短才找得到。**只按全串 grep 会把这些 key 判成"没有发出点"**，而那看起来
和"确实没有发出点"一模一样。

跑法：`.venv/bin/python scripts/alert_map.py`（加 `--json` 给下游用）
"""
from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ALERTS = REPO / ".qidian" / "alerts.jsonl"
DEFAULT_SRC = REPO / "src"


def read_counts(alerts_path: Path) -> Counter:
    """数每个 key 出现多少次。

    key 形如 `drain_dep_blocked` 或 `xml_tool_calls_recovered:1:run_command`，
    统一起见只取第一个 `:` 之前那段；没有 key 字段就当不聚合（不计）。
    读不动的行直接跳过 —— 告警文件是 append-only，半行是正常的。
    """
    counts: Counter = Counter()
    if not alerts_path.exists():
        return counts
    with alerts_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = (record.get("key") or "").split(":")[0]
            if k:
                counts[k] += 1
    return counts


# 只在**调用点的实参里**找 key。理由：key 的字面量还散落在注释、文档串、
# 白名单常量（`witness.py` 的 `_CRITICAL_ALERT_KEYS`）里 —— 全仓 grep 会把它们
# 一起算成"发出点"。实测 `decompose` 全仓命中 14 个文件，真正的发出点没那么多。
# 用 `ast` 拿准确的调用节点边界，不靠"前后多少字符"那种窗口猜。
#
# ⚠️ **不限于 `warn()`**：`phase_drift` 不是发告警发出来的，是
# `drifts.append({... "kind": "phase_drift" ...})` 攒出来的。只认 warn/emit/alert
# 会把它判成"回退命中"，然后**指到一堆只是提到这个词的文件上** —— 错得比"不知道"更坏。
# 所以放宽到"任何 Call 节点的实参里"，代价是有少量注释串混进来，可以接受。
# `witness.py` 的 `_CRITICAL_ALERT_KEYS = frozenset({...})` 是个**登记处**，不是发出点，
# 而它长得跟调用点一模一样（`frozenset(...)` 也是个 Call）。靠"构造字面量的内建函数"
# 把它排掉 —— 比按文件名整个排除干净：`witness.py` 里**确实还有**真发出点（`warn` 本身
# 就在那），整个排除会把真的也一起干掉。
# ⚠️ 实测 `observer_stalled_task` 就栽在登记处上：字面量只在白名单里出现过一次，
# 真发出点是 `_observer_worker.py` 那句 `key=f"observer_{alert.get('kind')}"` —— 拼的。
_LITERAL_CTORS = {"frozenset", "set", "tuple", "list", "dict", "str"}


def call_segments(src_dir: Path) -> dict[str, list[str]]:
    """→ {文件: [每个调用点的源码片段]}。语法错的文件跳过（不是本脚本的事）。"""
    out: dict[str, list[str]] = {}
    for p in sorted(src_dir.rglob("*.py")):
        name = str(p.relative_to(src_dir))
        try:
            body = p.read_text(encoding="utf-8")
            tree = ast.parse(body)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        lines = body.splitlines()
        segs = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not n.end_lineno:
                continue
            f = n.func
            if isinstance(f, ast.Name) and f.id in _LITERAL_CTORS:
                continue
            segs.append("\n".join(lines[n.lineno - 1:n.end_lineno]))
        if segs:
            out[name] = segs
    return out


# 回退命中超过这么多文件就**不报**，改报"查不到"。理由：key 拼不出来的那一类
# （比如 `observer_stalled_task` 是 `f"observer_{kind}"` 拼的，源码里全串和前缀都不存在），
# 回退会退到 `observer` 这种词上、命中十几个文件 —— 那**不是弱证据，是噪声**，
# 而且它看起来和"找到发出点"一样。**指错地方比说"不知道"坏**：人会照着错的文件去读。
_MAX_WEAK_HITS = 4


def find_emitters(key: str, calls: dict[str, list[str]]) -> tuple[list[str], bool]:
    """→ (发出点文件, 是否全串命中)。全串找不到时按 `_` 逐段截短重试。

    f-string 拼的 key 在源码里只剩前缀（`xml_tool_calls_recovered` 只有
    `f"xml_tool_calls_{_scope}:"`），截短才找得到。但截得太短就没判别力了
    （见 `_MAX_WEAK_HITS`），所以回退命中太多文件时返回空 —— 空是**诚实的**，
    读的人自己 grep 一下就知道。
    """
    parts = key.split("_")
    for n in range(len(parts), 0, -1):
        needle = "_".join(parts[:n])
        hits = [name for name, segs in calls.items()
                if any(needle in s for s in segs)]
        if n < len(parts) and len(hits) > _MAX_WEAK_HITS:
            return [], False        # 退到这么短才有命中 = 没判别力，不猜
        if hits:
            return sorted(hits), n == len(parts)
    return [], False


def build_map(calls: dict[str, list[str]], counts: Counter) -> list[dict]:
    """→ [{count, key, files, exact}, ...]，按 count 从多到少。"""
    out = []
    for key, count in counts.most_common():
        files, exact = find_emitters(key, calls)
        out.append({"count": count, "key": key, "files": files, "exact": exact})
    return out


def render_markdown(rows: list[dict]) -> str:
    lines = [
        "| 条数 | key（症状） | 发出它的文件（`scheduler/` 下） | 证据 |",
        "|---|---|---|---|",
    ]
    for r in rows:
        files = " · ".join(r["files"]) or "**查不到（key 是拼出来的）**"
        lines.append(f"| {r['count']} | `{r['key']}` | {files} | {'全串' if r['exact'] else '回退命中(弱)'} |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    paths = [a for a in argv[1:] if not a.startswith("--")]
    alerts = Path(paths[0]) if paths else DEFAULT_ALERTS
    counts = read_counts(alerts)
    if not counts:
        print(f"（{alerts} 里没有告警，或文件不存在）", file=sys.stderr)
        return 1
    rows = build_map(call_segments(DEFAULT_SRC), counts)
    print(json.dumps(rows, ensure_ascii=False, indent=1) if "--json" in argv else render_markdown(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
