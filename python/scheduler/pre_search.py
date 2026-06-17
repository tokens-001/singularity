"""pre_search.py — I 层预检 (知识库搜索 + MAGMA 多图记忆)

审计修了什么:
  - 强 D 阈值显式定义: decision 域前 K 条里 >=N 条 score>阈值 → 升 D (审计 6.2)
  - 超时/异常降级: I 层是辅助不是硬依赖, 挂了不阻塞主流程 (审计 2.4)

v2 升级 (MAGMA 多图记忆):
  - 知识库搜索后加多图记忆查询 (完整 Stage 1→4 流水线)
  - 记忆命中 → 路由偏置信号 (结构化, router 可编程消费)
  - 超时/异常降级: 记忆挂了不阻塞, 继续用知识库结果
"""

from __future__ import annotations
import json
import subprocess
from dataclasses import dataclass, field

from . import config
from .router import RouteResult


@dataclass
class MemoryHits:
    """MAGMA 多图记忆命中结果 (结构化, router 可编程消费)。"""
    narrative: list[dict] = field(default_factory=list)       # Stage 4 叙事合成结果
    intent: str = "semantic"                                    # 检测到的查询意图
    entity_matches: dict = field(default_factory=dict)          # {file: [task_ids]}
    semantic_baseline: list[dict] = field(default_factory=list) # 纯语义相似任务 (基线)
    graph_coverage: dict = field(default_factory=dict)          # {edge_type: count}


@dataclass
class PreSearchResult:
    escalated: bool = False          # 是否升 D
    top_decisions: list = None       # 命中的历史决策 (供 trace / neijinglu)
    skipped: bool = False            # 超时或异常降级
    reason: str = ""
    memory: MemoryHits = None        # MAGMA 多图记忆命中

    def __post_init__(self):
        if self.top_decisions is None:
            self.top_decisions = []
        if self.memory is None:
            self.memory = MemoryHits()


def pre_search(task: str, route_result: RouteResult, use_hybrid: bool = True) -> PreSearchResult:
    """调 search.py 查 decision 域 + MAGMA 多图记忆查询。"""
    res = PreSearchResult()

    # ── Step 1: 知识库搜索 (decision 域) ──
    if not config.SEARCH_SCRIPT.exists():
        res.skipped = True
        res.reason = "search.py 不存在"
    else:
        try:
            cmd = ["python3", str(config.SEARCH_SCRIPT), task,
                   "--domain", "decision", "--json"]
            if use_hybrid:
                cmd.append("--hybrid")

            # 使用 Popen + 线程定时器确保超时 (subprocess.run 的 timeout
            # 在 macOS 上可能因子进程僵尸而不触发)
            import threading
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True,  # 独立进程组, kill 时整组杀
            )
            timer = threading.Timer(config.PRE_SEARCH_TIMEOUT,
                                    lambda: proc.kill() if proc.poll() is None else None)
            timer.start()
            try:
                stdout, stderr = proc.communicate(timeout=config.PRE_SEARCH_TIMEOUT)
            finally:
                timer.cancel()

            if proc.returncode != 0:
                res.skipped = True
                rc = proc.returncode or -1
                res.reason = f"search.py exit={rc}: {(stderr or '')[:120]}"
            else:
                data = json.loads(stdout)
                results = data.get("results", [])[: config.STRONG_D_TOPK]
                res.top_decisions = [
                    {"id": r["id"], "title": r.get("title", ""), "score": r.get("score", 0)}
                    for r in results
                ]

                # 强 D 判定
                strong = [r for r in results if r.get("score", 0) > config.STRONG_D_MIN_SCORE]
                if len(strong) >= config.STRONG_D_MIN_HITS:
                    res.escalated = True
                    res.reason = (
                        f"强 D 命中: decision 域 {len(strong)} 条 score>"
                        f"{config.STRONG_D_MIN_SCORE} → 升 D"
                    )
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            res.skipped = True
            res.reason = f"超时 {config.PRE_SEARCH_TIMEOUT}s"
            try:
                proc.kill()
            except Exception:
                pass
        except (json.JSONDecodeError, KeyError) as e:
            res.skipped = True
            res.reason = f"解析失败: {e}"
        except Exception as e:  # noqa: BLE001
            res.skipped = True
            res.reason = f"未知异常: {e}"

    # ── Step 2: MAGMA 多图记忆查询 (完整 Stage 1→4) ──
    try:
        from . import memory as mem_mod

        mem_result = mem_mod.query(task)
        traversal = mem_result.get("traversal", {})
        res.memory = MemoryHits(
            narrative=traversal.get("narrative", []),
            intent=traversal.get("intent", "semantic"),
            entity_matches=mem_result.get("entity_matches", {}),
            semantic_baseline=mem_result.get("semantic_baseline", []),
            graph_coverage=traversal.get("graph_coverage", {}),
        )
    except Exception:
        pass  # 记忆挂了不阻塞

    return res


def apply_escalation(route_result: RouteResult, pre: PreSearchResult) -> None:
    """升 D 覆盖级别, 但保留 gate_required 标记。

    MAGMA 记忆增强: 结构化信号传给 router, 不做字符串拼接。
    """
    # 知识库强 D
    if pre.escalated and route_result.level != "D":
        route_result.matched_signals.append(f"pre_search 升 D: {pre.reason}")
        route_result.level = "D"

    if not pre.memory:
        return

    mem = pre.memory

    # ── MAGMA 遍历结果: 高评分记忆任务 → routing hint ──
    high_score = [r for r in mem.narrative if r.get("score", 0) >= 0.1]
    if high_score:
        tid = high_score[0]["task_id"]
        sources = high_score[0].get("graph_sources", [])
        route_result.matched_signals.append(
            f"MAGMA({mem.intent}): {tid[-8:]} "
            f"score={high_score[0]['score']:.3f} via {','.join(sources)}"
        )

    # ── 实体重叠: 文件级冲突预警 ──
    if mem.entity_matches:
        files = list(mem.entity_matches.keys())[:3]
        route_result.matched_signals.append(
            f"MAGMA entity: {', '.join(files)} 与历史任务重叠"
        )

    # ── 图覆盖: 各图命中统计 ──
    if mem.graph_coverage:
        cov_parts = [f"{edge}={n}" for edge, n in sorted(mem.graph_coverage.items())]
        route_result.matched_signals.append(f"MAGMA coverage: {', '.join(cov_parts)}")
