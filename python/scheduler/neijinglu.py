"""neijinglu.py — 交付完整性报告 (借天工内景录 + I010 教训)

审计修了什么 (审计 1.3):
  - "全量"重定义: 不是"跑完所有测试" (项目零测试), 而是"验证链所有已声明
    步骤都跑了, 每步产出明确 verdict"。neijinglu 检查验证链完整性,
    不是测试覆盖率。
  - 真实性标准对齐 validate.py: "可以放行, 但不把'通过'和'已验证'混为一谈;
    放行的必须标 unverified"。verdict=通过 + unverified 非空 = 诚实放行。
  - 永远带 snapshot_id (审计 4.4): 回滚是尽力而为, 把恢复责任交还给人。

v1 输出字段:
  - 任务原文 / 路由结果 / 变更文件清单
  - 验证结果 (validate verdict / gate / action)
  - unverified 项 (没验证的诚实清单)
  - rollback_reference (snapshot_id, 供人工恢复)
  - agent_output (executor 原始输出, 供 trace 审计)
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .router import RouteResult
from .snapshot import Snapshot
from .validator import ValidationReport
from .executors.base import ExecutorResult


@dataclass
class DeliveryReport:
    task: str
    route: RouteResult
    executor_result: ExecutorResult
    validation: ValidationReport
    snapshot: Snapshot
    final_status: str = "unknown"   # delivered | delivered_unverified | blocked | rolled_back
    pre_search_skipped: bool = False
    pre_search_reason: str = ""
    pre_search_top_decisions: list = None     # [{id, title, score}] 知识库命中
    pre_search_memory: dict = None            # MAGMA 记忆命中 (MemoryHits.to_dict)

    @classmethod
    def from_dict(cls, d: dict) -> "DeliveryReport":
        """从 JSON dict 重建 (简化版, 仅字段映射)。"""
        route_data = d.get("route", {})
        route = RouteResult(
            level=route_data.get("level", "E"),
            gate_required=route_data.get("gate_required", False),
            task_type=route_data.get("task_type", "default"),
            matched_signals=route_data.get("matched_signals", []),
        )
        from .validator import ValidationReport
        val_data = d.get("validation", {})
        validation = ValidationReport(
            verdict=val_data.get("verdict", ""),
            action=val_data.get("action", ""),
            validate_verdict=val_data.get("validate_verdict", ""),
            validate_reason=val_data.get("validate_reason", ""),
            gate_passed=val_data.get("gate_passed"),
            gate_message=val_data.get("gate_message", ""),
            turns_used=val_data.get("turns_used", 0),
            unverified=val_data.get("unverified", []),
        )
        from .snapshot import Snapshot
        snapshot = Snapshot(
            id=d.get("rollback_reference", ""),
            method=d.get("snapshot_method", ""),
            ref=d.get("snapshot_ref", ""),
            created_at=0.0,
        )
        from .dispatcher import ExecutorResult
        exec_result = ExecutorResult(
            success=True,  # 能从 trace 重建说明 executor 当时成功了
            raw_output=d.get("agent_output", ""),
            changed_files=d.get("changed_files", []),
            patch_path=d.get("patch_path", ""),
            token_count=d.get("token_count", 0),
            elapsed=d.get("elapsed", 0.0),
            error=d.get("error", ""),
            error_kind=d.get("error_kind", ""),
        )
        return cls(
            task=d.get("task", ""),
            route=route,
            executor_result=exec_result,
            validation=validation,
            snapshot=snapshot,
            final_status=d.get("final_status", "unknown"),
            pre_search_skipped=d.get("pre_search", {}).get("skipped", False),
            pre_search_reason=d.get("pre_search", {}).get("reason", ""),
            pre_search_top_decisions=d.get("pre_search", {}).get("top_decisions", []),
            pre_search_memory=d.get("pre_search", {}).get("memory"),
        )

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "final_status": self.final_status,
            "route": {
                "level": self.route.level,
                "gate_required": self.route.gate_required,
                "task_type": self.route.task_type,
                "matched_signals": self.route.matched_signals,
            },
            "pre_search": {
                "skipped": self.pre_search_skipped,
                "reason": self.pre_search_reason,
                "top_decisions": self.pre_search_top_decisions or [],
                "memory": self.pre_search_memory or {},
            },
            "changed_files": self.executor_result.changed_files,
            "patch_path": self.executor_result.patch_path,
            "validation": {
                "verdict": self.validation.verdict,
                "action": self.validation.action,
                "validate_verdict": self.validation.validate_verdict,
                "validate_reason": self.validation.validate_reason,
                "gate_passed": self.validation.gate_passed,
                "gate_message": self.validation.gate_message,
                "turns_used": self.validation.turns_used,
            },
            "unverified": self.validation.unverified,
            "rollback_reference": self.snapshot.id,
            "snapshot_method": self.snapshot.method,
            "snapshot_ref": self.snapshot.ref,
            "agent_output": self.executor_result.raw_output if self.executor_result else "",
            "token_count": self.executor_result.token_count if self.executor_result else 0,
            "elapsed": self.executor_result.elapsed if self.executor_result else 0.0,
        }


def build_report(
    task: str,
    route: RouteResult,
    executor_result: ExecutorResult,
    validation: ValidationReport,
    snapshot: Snapshot,
    pre_search_skipped: bool = False,
    pre_search_reason: str = "",
    rolled_back: bool = False,
    pre_search_top_decisions: list = None,
    pre_search_memory: dict = None,
) -> DeliveryReport:
    """组装交付报告。"""
    report = DeliveryReport(
        task=task,
        route=route,
        executor_result=executor_result,
        validation=validation,
        snapshot=snapshot,
        pre_search_skipped=pre_search_skipped,
        pre_search_reason=pre_search_reason,
        pre_search_top_decisions=pre_search_top_decisions or [],
        pre_search_memory=pre_search_memory or {},
    )

    # 最终状态判定 (审计 1.3: 通过≠已验证)
    if rolled_back:
        report.final_status = "rolled_back"
    elif validation.action == "rollback":
        report.final_status = "rolled_back"
    elif validation.action in ("abort",):
        report.final_status = "blocked"
    elif validation.verdict == "通过":
        # 通过但有 unverified 项 → 诚实放行 (审计 1.3)
        if validation.unverified:
            report.final_status = "delivered_unverified"
        else:
            report.final_status = "delivered"
    else:
        report.final_status = "blocked"

    return report


def format_report(report: DeliveryReport) -> str:
    """人可读的 markdown 报告。"""
    d = report.to_dict()
    lines = [
        "## 奇点调度交付报告",
        f"**任务:** {d['task']}",
        f"**最终状态:** {d['final_status']}",
        "",
        "### 路由",
        f"- 复杂度: {d['route']['level']}",
        f"- 任务类型: {d['route']['task_type']}",
        f"- gate_required: {d['route']['gate_required']}",
        f"- 命中信号: {'; '.join(d['route']['matched_signals']) or '无'}",
    ]

    if d["pre_search"]["skipped"]:
        lines.append(f"- ⚠️ I层预检跳过: {d['pre_search']['reason']}")

    lines += [
        "",
        "### 变更",
        f"- 文件: {d['changed_files'] or '无'}",
    ]
    if d["patch_path"]:
        lines.append(f"- E+ patch (未apply): {d['patch_path']}")

    lines += [
        "",
        "### 验证",
        f"- validate verdict: {d['validation']['validate_verdict']}",
        f"- gate: {d['validation']['gate_passed']} — {d['validation']['gate_message']}" if d['validation']['gate_passed'] is not None else "- gate: 未触发",
        f"- 汇总 verdict: {d['validation']['verdict']}",
        f"- action: {d['validation']['action']}",
        f"- 打回轮次: {d['validation']['turns_used']}",
    ]

    if d["unverified"]:
        lines += ["", "### ⚠️ unverified (未验证项, 诚实标注)"]
        for u in d["unverified"]:
            lines.append(f"- {u}")

    lines += [
        "",
        "### 回滚",
        f"- snapshot_id: `{d['rollback_reference']}`",
        f"- method: {d['snapshot_method']}",
        f"- ref: `{d['snapshot_ref']}`",
        f"- 恢复命令: `python3 -m scheduler rollback {d['rollback_reference']}`",
    ]

    return "\n".join(lines)


def save_trace(report: DeliveryReport, task_id: str) -> Path:
    """完整 trace 存 json, 供事后审计 (I010: 留证据不留断言)。

    选择性摄入 (受 Omni-SimpleMem 启发)：
    如果同类型 + 同 failure_mode 的 trace 已连续出现 ≥3 次，
    则保存轻量版（裁剪 raw_output，避免冗余膨胀）。
    """
    trace_path = config_trace_path(task_id)
    data = report.to_dict()

    # 选择性摄入去重
    if _is_redundant_failure(data):
        # 轻量存储：裁剪大字段
        if "agent_output" in data:
            data["agent_output"] = data["agent_output"][:200] + (
                "…[已截断: 同类失败重复]" if len(data["agent_output"]) > 200 else ""
            )
        if "changed_files" in data:
            data["changed_files"] = data["changed_files"][:5]
        data["_dedup"] = True

    trace_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return trace_path


def _is_redundant_failure(data: dict, lookback: int = 3) -> bool:
    """检查当前 trace 是否是最近 lookback 条中重复出现的同类失败。

    条件：
    1. 当前 trace 是失败状态
    2. 最近 lookback 条同 task_type 的 trace 中，
       有 ≥ lookback 条具有相同的 failure 特征（task_type + validation 一致）
    """
    from . import config
    import os as _os

    trace_dir = config.TRACE_DIR
    if not trace_dir.exists():
        return False

    # 提取当前 trace 的判重特征
    route = data.get("route", {}) or {}
    task_type = route.get("task_type", "default")
    validation = data.get("validation", {}) or {}
    verdict = validation.get("verdict", "")
    action = validation.get("action", "")
    # 只对非 pass 的 trace 做去重
    if action == "pass" and not verdict:
        return False
    if verdict == "pass":
        return False

    sig = (task_type, verdict, action)

    # 收集最近 20 条 trace 的判重特征
    try:
        files = sorted(
            trace_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:20]
    except OSError:
        return False

    match_count = 0
    for f in files[:lookback * 3]:  # 在最近 lookback*3 条中查找
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        rt = d.get("route", {}) or {}
        vt = d.get("validation", {}) or {}
        other_sig = (rt.get("task_type", ""), vt.get("verdict", ""), vt.get("action", ""))
        if other_sig == sig:
            match_count += 1
            if match_count >= lookback:
                return True

    return False


def config_trace_path(task_id: str) -> Path:
    from . import config
    return config.TRACE_DIR / f"{task_id}.json"
