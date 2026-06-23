#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
I 层校验器 — 诚实版
====================
输入候选方案 → 多域并行检索 → 返回布尔+证据清单
不做伪精确评分，不做否定词假矛盾检测。
"""

import sys
import re
from pathlib import Path
from collections import defaultdict

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))

from core import KnowledgeSearchEngine


class Validator:
    """I 层校验器 —— 检索 + 提示，不伪造精确度"""

    def __init__(self, engine: KnowledgeSearchEngine = None):
        if engine is None:
            engine = KnowledgeSearchEngine()
            engine.load()
        self.engine = engine

    # ═══════════════════════════════════════
    # 输入校验（原 schema_gate —— 缩为一次 if 判断）
    # ═══════════════════════════════════════

    def _check_input(self, text: str) -> dict:
        """检查输入是否足够形成候选方案"""
        text = text.strip()
        issues = []

        if len(text) < 10:
            issues.append("输入太短，无法形成候选方案")
        if any(marker in text.upper() for marker in ["TODO", "FIXME", "TBD", "占位"]):
            issues.append("含占位符，方案未完成")

        return {
            "valid": len(issues) == 0,
            "issues": issues,
        }

    # ═══════════════════════════════════════
    # 主校验入口
    # ═══════════════════════════════════════

    def validate(self, candidate: str) -> dict:
        """
        校验候选方案

        返回:
          {
            "candidate": str,
            "input_ok": bool,
            "input_issues": [...],

            # 证据 —— 不是判决
            "principles": [...],       # 相关原则（可能相关，非"必须满足"）
            "decisions": [...],        # 相关历史决策
            "cases": [...],            # 相关案卷/洞察
            "questions": [...],        # 牵动未决问题

            # 提示 —— 不是矛盾检测
            "lexical_hints": [...],    # 词法信号（和某决策共享主题+含否定词）
            "status_notes": [...],     # 状态提醒（原则是0用户验证等）

            # 结论 —— 只有三档
            "verdict": str,            # "注意" | "人工复核" | "信息不足"
            "verdict_reason": str,
          }
        """
        # Step 0: 输入校验
        input_check = self._check_input(candidate)
        if not input_check["valid"]:
            return {
                "candidate": candidate[:200],
                "input_ok": False,
                "input_issues": input_check["issues"],
                "principles": [],
                "decisions": [],
                "cases": [],
                "questions": [],
                "lexical_hints": [],
                "status_notes": [],
                "verdict": "信息不足",
                "verdict_reason": "; ".join(input_check["issues"]),
            }

        # Step 1: 多域检索（证据收集）
        searches = {
            "principle": self.engine.search(candidate, domain="principle", max_results=3),
            "decision": self.engine.search(candidate, domain="decision", max_results=3),
            "case": self.engine.search(candidate, domain="case", max_results=3),
            "insight": self.engine.search(candidate, domain="insight", max_results=2),
            "question": self.engine.search(candidate, domain="question", max_results=2),
        }

        # Step 2: 组装证据
        principles = self._summarize_results(searches["principle"])
        decisions = self._summarize_results(searches["decision"])
        cases = self._combine(searches["case"], searches["insight"])
        questions = self._summarize_results(searches["question"])

        # Step 3: 词法提示（不是矛盾检测！）
        lexical_hints = self._lexical_signals(candidate, decisions)

        # Step 4: 状态提醒
        status_notes = self._status_notes(principles)

        # Step 5: 判定 —— 不是分数，是三档结论
        verdict, verdict_reason = self._decide(
            lexical_hints, status_notes, questions, decisions
        )

        return {
            "candidate": candidate[:200],
            "input_ok": True,
            "input_issues": [],
            "principles": principles,
            "decisions": decisions,
            "cases": cases,
            "questions": questions,
            "lexical_hints": lexical_hints,
            "status_notes": status_notes,
            "verdict": verdict,
            "verdict_reason": verdict_reason,
        }

    # ═══════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════

    def _summarize_results(self, result: dict) -> list:
        """搜索结果 → 简化列表"""
        return [
            {
                "id": r["id"],
                "type": r["type"],
                "title": r["title"],
                "path": r["path"],
                "score": r.get("score", 0),
            }
            for r in result.get("results", [])
        ]

    def _combine(self, *result_dicts) -> list:
        """合并多个搜索结果，去重，按 score 降序"""
        seen = set()
        combined = []
        for rd in result_dicts:
            for item in self._summarize_results(rd):
                if item["id"] not in seen:
                    seen.add(item["id"])
                    combined.append(item)
        return sorted(combined, key=lambda x: -x["score"])

    def _lexical_signals(self, candidate: str, decisions: list) -> list:
        """
        词法信号 —— 诚实的字符串级提示，不冒充"矛盾检测"。

        信号规则: 候选和某历史决策共享主题词 ≥2 个，
        且候选含否定词而该决策不含（反过来同理）。
        这仅表示"值得人工看一眼"，不表示真实矛盾。
        """
        neg_words = ["不", "否", "别", "非", "停止", "暂停", "放弃", "取消", "暂缓", "推迟"]
        cand_tokens = set(self.engine.tokenizer.tokenize(candidate))
        cand_has_neg = any(w in candidate for w in neg_words)

        signals = []
        for d in decisions:
            d_title = d.get("title", "")
            d_tokens = set(self.engine.tokenizer.tokenize(d_title))
            overlap = cand_tokens & d_tokens

            if len(overlap) >= 2:
                d_has_neg = any(w in d_title for w in neg_words)

                # 词法方向不一致 → 给出提示（不强称矛盾）
                if cand_has_neg != d_has_neg:
                    signals.append(
                        {
                            "doc_id": d["id"],
                            "shared_tokens": list(overlap)[:5],
                            "note": f"候选含否定词而 {d['id']} 不含（反之亦然），建议人工比对方向",
                            "doc_title": d_title,
                        }
                    )

        return signals

    def _status_notes(self, principles: list) -> list:
        """状态提醒 —— 原则未经验证时给软提示"""
        notes = []
        for p in principles:
            pid = p.get("id", "")
            # P 开头且来自 research/insights/ 的原则都是"自测通过（0真实用户）"
            if pid.startswith("P"):
                notes.append(
                    {
                        "doc_id": pid,
                        "status": "自测通过（0真实用户）",
                        "note": f"原则 {pid} 未经真实用户验证，参考但不作为硬阻断依据",
                    }
                )
        return notes

    def _decide(self, lexical_hints, status_notes, questions, decisions) -> tuple:
        """三档判定"""
        reasons = []

        if lexical_hints:
            doc_ids = [h["doc_id"] for h in lexical_hints]
            reasons.append(f"词法信号: 和 {', '.join(doc_ids)} 共享主题但方向词法不一致，建议人工比对")

        if status_notes:
            reasons.append(f"{len(status_notes)} 条原则为自测通过（0真实用户），不做硬阻断")

        if questions:
            q_ids = [q["id"] for q in questions]
            reasons.append(f"牵动未决问题: {', '.join(q_ids)}")

        if not reasons:
            return "注意", "未发现明显信号，相关历史决策见 evidence"

        # 有词法冲突 → 人工复核（不是"阻断"，是"建议看一眼"）
        if lexical_hints:
            return "人工复核", "; ".join(reasons)

        return "注意", "; ".join(reasons)


# ═══════════════════════════════════════
# 输出格式化
# ═══════════════════════════════════════

def format_validation(result: dict) -> str:
    """校验报告 → 可读文本"""
    lines = []
    icon = {"人工复核": "⚠️", "注意": "📋", "信息不足": "❓"}

    lines.append("## I 层校验报告（诚实版）")
    lines.append(f"**候选:** {result['candidate']}")
    lines.append(f"**判定:** {icon.get(result['verdict'], '?')} **{result['verdict']}**")
    lines.append(f"**原因:** {result['verdict_reason']}")
    lines.append("")

    # 输入问题
    if result["input_issues"]:
        lines.append("### 输入问题")
        for i in result["input_issues"]:
            lines.append(f"- ❌ {i}")
        lines.append("")

    # 词法信号
    if result["lexical_hints"]:
        lines.append("### ⚠️ 词法信号（建议人工比对方向）")
        for h in result["lexical_hints"]:
            lines.append(f"- **{h['doc_id']}** {h['doc_title']} — {h['note']}")
            lines.append(f"  共享词: {', '.join(h['shared_tokens'])}")
        lines.append("")

    # 状态提醒
    if result["status_notes"]:
        lines.append("### 📝 状态提醒")
        for n in result["status_notes"]:
            lines.append(f"- **{n['doc_id']}**: {n['note']}")
        lines.append("")

    # 证据
    for label, key in [
        ("相关原则", "principles"),
        ("历史决策", "decisions"),
        ("相关案卷/洞察", "cases"),
        ("牵动未决问题", "questions"),
    ]:
        items = result.get(key, [])
        if items:
            lines.append(f"### {label}")
            for item in items:
                lines.append(
                    f"- **{item['id']}** {item['title']} "
                    f"({item['type']}, score={item.get('score', 0):.1f})"
                )
            lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════
# CLI
# ═══════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="I 层校验器（诚实版）")
    parser.add_argument("candidate", help="候选方案文本")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    validator = Validator()
    result = validator.validate(args.candidate)

    if args.json:
        import json

        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_validation(result))
