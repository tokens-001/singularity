"""chancellor.py — Singularity：报错总管。

不调 LLM。纯规则分类 + 白话翻译。小事自理，大事奏报。
"""

from __future__ import annotations
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from singularity.scheduler import config


# ── 严重程度 ──
# noise:   单次重试、正常 feedback → 不报
# routine: 首次失败、可修 → 自动标重试
# alert:   2+次失败、E+已耗尽、架构冲突 → 白话奏报
# critical: merge 冲突、核心文件改动、数据风险 → 立即奏报

@dataclass
class Report:
    severity: str           # "noise" | "routine" | "alert" | "critical"
    title: str              # 一句话
    what: str               # 白话解释发生了什么
    why: str                # 为什么值得关注
    suggestion: str         # 建议怎么处理
    task_ids: list[str] = field(default_factory=list)
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "severity": self.severity, "title": self.title,
            "what": self.what, "why": self.why, "suggestion": self.suggestion,
            "task_ids": self.task_ids, "ts": self.ts,
        }


# ── 分类规则 ──

_CORE_FILES = {"core.py", "tokenizer.py", "graph.py", "search.py", "config.py", "tracker.py"}
def assess(task_desc: str, term_reason: str, changed_files: list[str] = None,
           retry_count: int = 0, agent_tried: list[str] = None) -> Report:
    """分析一个任务结果，决定是否奏报。

    Returns:
        Report with severity. noise/routine → 自理，alert/critical → 奏报。
    """
    changed_files = changed_files or []
    agent_tried = agent_tried or []
    now = time.time()

    # ── critical: 核心文件被改 ──
    for f in changed_files:
        if Path(f).name in _CORE_FILES:
            return Report(
                severity="critical",
                title=f"核心文件 {Path(f).name} 被修改",
                what=f"任务「{task_desc[:60]}」改动了 {f}。这是引擎/调度核心文件，改错可能导致调度器瘫痪。",
                why="核心文件改动风险极高，需立即确认是否允许。",
                suggestion="检查改动内容。如果是不小心改的，回滚。如果是有意为之，确认改动正确后放行。",
                task_ids=[], ts=now,
            )

    # ── critical: merge 冲突 ──
    if "merge_conflict" in term_reason or "conflict" in term_reason.lower():
        return Report(
            severity="critical",
            title="合并冲突，需人工裁决",
            what=f"任务「{task_desc[:60]}」和其他任务改了同一文件的同一区域。自动合并失败。",
            why="合并冲突不能自动解决，两个 agent 的工作有交集。必须你来决定保留谁的改动。",
            suggestion="去干预面板看冲突详情，选择保留哪个版本，或者让两个 agent 重新协调。",
            task_ids=[], ts=now,
        )

    # ── alert: 升级链耗尽 ──
    if "escalation_exhausted" in term_reason:
        tried = ", ".join(agent_tried) if agent_tried else "全部 agent"
        return Report(
            severity="alert",
            title="所有模型都试过了，没搞定",
            what=f"任务「{task_desc[:60]}」从 E 升到 D，{tried} 都试了，还是失败了。D 层已给出分析方案，已自动生成 E+ 修复任务。",
            why="这是当前系统能力的天花板。可能任务描述不清、约束太严、或者需要你亲自看一下。",
            suggestion="查看 D 层的分析方案。如果方案可行，让修复任务跑完。如果方案也不行，重新描述需求或放宽约束。",
            task_ids=[], ts=now,
        )

    # ── alert: 重复失败 (2+) ──
    if retry_count >= 2:
        tried = ", ".join(agent_tried) if agent_tried else "多个 agent"
        return Report(
            severity="alert",
            title=f"修了 {retry_count} 次还没好",
            what=f"任务「{task_desc[:60]}」已经让 {tried} 试了 {retry_count} 次，每次修复后测试还是不通过。",
            why="同一个任务反复失败说明问题可能不在 agent 能力，而在需求描述、现有代码质量、或者隐藏的耦合。",
            suggestion="重新审视任务描述是否清晰。检查是否有隐藏的依赖关系。考虑让 D 层出完整方案后再执行。",
            task_ids=[], ts=now,
        )

    # ── routine: 首次失败 ──
    if "failed" in term_reason.lower() and retry_count <= 1:
        return Report(
            severity="routine",
            title="小问题，已自动安排重试",
            what=f"任务「{task_desc[:60]}」第一次没跑通。",
            why="可能是瞬态问题。已自动切换到备选 agent 重试。",
            suggestion="不用管，系统会自动处理。",
            task_ids=[], ts=now,
        )

    # ── noise: 其他 ──
    return Report(
        severity="noise",
        title="",
        what="",
        why="",
        suggestion="",
        task_ids=[], ts=now,
    )


# ── 持久化 ──

def _reports_dir() -> Path:
    d = config.QIDIAN_DIR / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_report(report: Report) -> str:
    """保存奏报，返回 report_id。noise 不存档。"""
    if report.severity == "noise":
        return ""
    rid = f"{int(time.time() * 1000)}"
    report.ts = report.ts or time.time()
    p = _reports_dir() / f"{rid}.json"
    p.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return rid


def list_reports(limit: int = 30, min_severity: str = "routine") -> list[dict]:
    """列出奏报，按时间倒序，可按严重程度过滤。"""
    severity_order = {"critical": 0, "alert": 1, "routine": 2, "noise": 3}
    min_level = severity_order.get(min_severity, 3)
    reports = []
    for p in sorted(_reports_dir().glob("*.json"), reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if severity_order.get(d.get("severity", "noise"), 3) <= min_level:
                d["id"] = p.stem
                reports.append(d)
        except (json.JSONDecodeError, OSError):
            continue
    return reports[:limit]


def recent_critical() -> list[dict]:
    """最近的关键奏报，用于面板快速查看。"""
    return list_reports(limit=5, min_severity="alert")

def dismiss_report(report_id: str) -> bool:
    """删除指定奏报。"""
    p = _reports_dir() / f"{report_id}.json"
    if p.exists():
        try: p.unlink(); return True
        except OSError: pass
    return False
