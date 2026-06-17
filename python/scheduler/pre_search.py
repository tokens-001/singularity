"""pre_search.py — I 层预检 (调 search.py 查历史决策)

审计修了什么:
  - 强 D 阈值显式定义: decision 域前 K 条里 >=N 条 score>阈值 → 升 D (审计 6.2)
  - 超时/异常降级: I 层是辅助不是硬依赖, 挂了不阻塞主流程 (审计 2.4)
  - 升 D 覆盖关键词结果, 但不覆盖 gate_required 标记 (审计 2.1 附加标记独立)

v1 边界:
  - 只查 decision 域 (历史决策); 不查 principle (原则是 0 用户验证, 软提示用)
  - 超时即跳过, neijinglu 标 pre_search_skipped
"""

from __future__ import annotations
import json
import subprocess
from dataclasses import dataclass

from . import config
from .router import RouteResult


@dataclass
class PreSearchResult:
    escalated: bool = False          # 是否升 D
    top_decisions: list = None       # 命中的历史决策 (供 trace / neijinglu)
    skipped: bool = False            # 超时或异常降级
    reason: str = ""

    def __post_init__(self):
        if self.top_decisions is None:
            self.top_decisions = []


def pre_search(task: str, route_result: RouteResult, use_hybrid: bool = False) -> PreSearchResult:
    """调 search.py 查 decision 域, 强 D 命中则升 D。use_hybrid → BM25+语义 RRF。"""
    res = PreSearchResult()
    if not config.SEARCH_SCRIPT.exists():
        res.skipped = True
        res.reason = "search.py 不存在"
        return res

    try:
        cmd = ["python3", str(config.SEARCH_SCRIPT), task,
               "--domain", "decision", "--json"]
        if use_hybrid:
            cmd.append("--hybrid")
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=config.PRE_SEARCH_TIMEOUT,
        )
        if proc.returncode != 0:
            res.skipped = True
            res.reason = f"search.py exit={proc.returncode}: {proc.stderr[:120]}"
            return res

        data = json.loads(proc.stdout)
        results = data.get("results", [])[: config.STRONG_D_TOPK]
        res.top_decisions = [
            {"id": r["id"], "title": r.get("title", ""), "score": r.get("score", 0)}
            for r in results
        ]

        # 强 D 判定 (审计 6.2): 前 K 条里 >=N 条 score 超阈值
        strong = [r for r in results if r.get("score", 0) > config.STRONG_D_MIN_SCORE]
        if len(strong) >= config.STRONG_D_MIN_HITS:
            res.escalated = True
            res.reason = (
                f"强 D 命中: decision 域 {len(strong)} 条 score>"
                f"{config.STRONG_D_MIN_SCORE} → 升 D"
            )
    except subprocess.TimeoutExpired:
        res.skipped = True
        res.reason = f"超时 {config.PRE_SEARCH_TIMEOUT}s, 降级用关键词结果"
    except (json.JSONDecodeError, KeyError) as e:
        res.skipped = True
        res.reason = f"解析失败: {e}"
    except Exception as e:  # noqa: BLE001 — I 层挂了不能拖垮主流程
        res.skipped = True
        res.reason = f"未知异常: {e}"

    return res


def apply_escalation(route_result: RouteResult, pre: PreSearchResult) -> None:
    """升 D 覆盖级别, 但保留 gate_required 标记 (审计 2.1)。"""
    if pre.escalated and route_result.level != "D":
        route_result.matched_signals.append(
            f"pre_search 升 D: {pre.reason}"
        )
        route_result.level = "D"
