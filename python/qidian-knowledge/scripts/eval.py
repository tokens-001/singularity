#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评测脚本 + SkillOpt Gate 模式
================================
- 加载 golden_set.jsonl → 对每条查询跑检索 → 计算 recall@k
- --gate: 对比基线，Recall@3 下降则退出非零（拒绝变更）
- --save-baseline: 将当前分数写入基线
- 基线文件: data/.eval_baseline（JSON，一行）

Gate 模式借鉴 SkillOpt 第 4 步：只接受在验证集上严格改进的编辑。
改 core.py / tokenizer.py / 域路由词表后必须过闸。
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))

from core import KnowledgeSearchEngine


def load_golden_set(path: Path) -> list:
    """加载 golden set（每行一个 JSON）"""
    queries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                queries.append(json.loads(line))
    return queries


def evaluate(engine: KnowledgeSearchEngine, golden_path: Path, k: int = 3) -> dict:
    """评测 recall@k"""
    queries = load_golden_set(golden_path)
    results = []
    total_hits = 0
    total_expected = 0

    for q in queries:
        query = q["query"]
        expect_ids = set(q["expect"])
        domain = q.get("domain")

        search_result = engine.search(query, domain=domain)
        hit_ids = {r["id"] for r in search_result["results"][:k]}
        hits = len(hit_ids & expect_ids)

        total_hits += hits
        total_expected += len(expect_ids)

        results.append(
            {
                "query": query,
                "expect": list(expect_ids),
                "hits": list(hit_ids & expect_ids),
                "misses": list(expect_ids - hit_ids),
                "returned": [r["id"] for r in search_result["results"][:k]],
                "recall": hits / len(expect_ids) if expect_ids else 0,
            }
        )

    recall_at_k = total_hits / total_expected if total_expected > 0 else 0

    return {
        "k": k,
        "total_queries": len(queries),
        "total_hits": total_hits,
        "total_expected": total_expected,
        "recall_at_k": round(recall_at_k, 4),
        "per_query": results,
    }


def print_report(eval_result: dict):
    """打印评测报告"""
    k = eval_result["k"]
    recall = eval_result["recall_at_k"]

    print(f"\n{'='*60}")
    print(f"  Golden Set 评测报告")
    print(f"{'='*60}")
    print(f"  查询数:  {eval_result['total_queries']}")
    print(f"  期望命中: {eval_result['total_expected']}")
    print(f"  实际命中: {eval_result['total_hits']}")
    print(f"  Recall@{k}: {recall:.2%}")
    print(f"{'='*60}")

    misses = [r for r in eval_result["per_query"] if r["misses"]]
    if misses:
        print(f"\n  未命中 ({len(misses)} 条):")
        for r in misses:
            print(f"    ✗ 「{r['query']}」")
            print(f"      期望: {r['expect']}  返回: {r['returned']}")
            print(f"      命中: {r['hits'] or '无'}  漏: {r['misses']}")

    perfect = len(eval_result["per_query"]) - len(misses)
    print(f"\n  完全命中: {perfect}/{eval_result['total_queries']}")
    print(f"{'='*60}\n")

    if recall >= 0.8:
        print("  ✅ Recall@3 达标 (≥0.8)")
    else:
        print(f"  ❌ Recall@3 未达标 ({recall:.2%} < 0.8)")

    print()


BASELINE_PATH = Path(__file__).parent.parent / "data" / ".eval_baseline"
DEFAULT_THRESHOLD = 0.80


def load_baseline() -> dict | None:
    """加载基线分数"""
    if not BASELINE_PATH.exists():
        return None
    try:
        with open(BASELINE_PATH, "r", encoding="utf-8") as f:
            return json.loads(f.readline())
    except (json.JSONDecodeError, StopIteration):
        return None


def save_baseline(recall: float, k: int, queries: int, mode: str = "bm25"):
    """保存当前分数为基线"""
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    baseline = {
        "recall_at_k": round(recall, 4),
        "k": k,
        "total_queries": queries,
        "mode": mode,
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps(baseline, ensure_ascii=False) + "\n")


def run_gate(eval_result: dict, threshold: float = None) -> tuple[bool, str]:
    """
    SkillOpt Gate 模式：对比基线，只接受不退化。

    返回 (passed: bool, message: str)
    - 无基线时：Recall >= threshold 即通过
    - 有基线时：Recall >= 基线 且 Recall >= threshold 即通过
    """
    if threshold is None:
        threshold = DEFAULT_THRESHOLD

    recall = eval_result["recall_at_k"]
    baseline = load_baseline()
    k = eval_result["k"]

    messages = []

    if baseline is None:
        # 无基线 → 只检查阈值
        msg = f"无基线 → 检查 Recall@{k} ≥ {threshold:.0%}"
        messages.append(msg)
        if recall >= threshold:
            return True, f"{msg}: ✅ {recall:.2%} ≥ {threshold:.0%}"
        else:
            return False, f"{msg}: ❌ {recall:.2%} < {threshold:.0%}"

    base_recall = baseline["recall_at_k"]
    base_queries = baseline.get("total_queries", "?")
    base_saved = baseline.get("saved_at", "?")
    baseline_ok = recall >= base_recall
    threshold_ok = recall >= threshold

    # 基线对比
    delta = recall - base_recall
    delta_str = f"+{delta:.4f}" if delta >= 0 else f"{delta:.4f}"
    msg_base = (
        f"基线 {base_recall:.4f} ({base_queries}条, {base_saved}) → "
        f"当前 {recall:.4f} ({delta_str})"
    )
    messages.append(msg_base)

    # 阈值检查
    msg_thresh = f"阈值 Recall@{k} ≥ {threshold:.0%}: {'✅' if threshold_ok else '❌'} {recall:.2%}"
    messages.append(msg_thresh)

    if baseline_ok and threshold_ok:
        return True, " | ".join(messages)
    elif not baseline_ok:
        # 即使仅低于基线（仍高于阈值），也拒绝 —— SkillOpt 的"严格改进"原则
        return False, f"❌ 退化！{msg_base} | {msg_thresh}"
    else:
        return False, f"❌ 低于阈值！{' | '.join(messages)}"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Golden Set 评测 + SkillOpt Gate")
    parser.add_argument(
        "--golden", "-g",
        type=str,
        default=str(Path(__file__).parent.parent / "data" / "golden_set.jsonl"),
        help="Golden set 路径",
    )
    parser.add_argument(
        "--k", "-k", type=int, default=3, help="Recall@k (默认 3)"
    )
    parser.add_argument(
        "--gate", action="store_true",
        help="Gate 模式：对比基线，Recall 下降则 exit 1",
    )
    parser.add_argument(
        "--save-baseline", action="store_true",
        help="保存当前分数为基线",
    )
    parser.add_argument(
        "--threshold", "-t", type=float, default=DEFAULT_THRESHOLD,
        help=f"Gate 最低阈值（默认 {DEFAULT_THRESHOLD}），无基线时使用",
    )
    parser.add_argument(
        "--mode", "-m", type=str, default="bm25",
        choices=["bm25", "hybrid", "semantic"],
        help="搜索模式: bm25 (默认) | hybrid | semantic",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="输出 JSON（含 gate 结论）",
    )
    args = parser.parse_args()

    print("加载引擎...", file=sys.stderr)
    engine = KnowledgeSearchEngine()
    engine.load()
    if args.mode == "semantic":
        engine.enable_semantic_only()
    elif args.mode == "hybrid":
        engine.enable_hybrid()

    golden_path = Path(args.golden)
    print(f"评测 golden set: {golden_path}", file=sys.stderr)
    result = evaluate(engine, golden_path, k=args.k)

    if args.gate:
        passed, msg = run_gate(result, threshold=args.threshold)
        result["gate"] = {"passed": passed, "message": msg}
        result["mode"] = args.mode

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_report(result)
            print(f"\n🔒 GATE ({args.mode}): {msg}")

        sys.exit(0 if passed else 1)
    elif args.save_baseline:
        save_baseline(
            result["recall_at_k"], result["k"], result["total_queries"],
            mode=args.mode,
        )
        print(f"基线已保存 ({args.mode}): Recall@{result['k']} = {result['recall_at_k']:.4f} "
              f"({result['total_queries']} 条查询)",
              file=sys.stderr)
    else:
        print_report(result)
