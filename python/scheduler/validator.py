"""validator.py — 验证闭环

审计修了什么 (审计 3.1 / 3.5 / 1a):
  - verdict 映射表对齐 validate.py 真实输出 (审计 3.1):
      validate.py 输出 "人工复核" / "注意" / "信息不足" 三档,
      原稿映射表漏了"信息不足"导致空输出被放行 —— Opus 审查补上,
      映射 retry 而非 pass。verdict 值不在白名单 → 按未知处理 (通过+unverified)
  - 三套规则合并 (审计 3.5): validate verdict + gate + 任务类型追加,
      按"最硬者优先"合并, 明确执行序与短路:
        gate 失败 > 人工复核 > 注意/未知
      gate 失败是唯一硬阻断 (有客观数据 Recall@3), 其余软打回
  - gate_required 触发 (审计 1a): 命中引擎核心文件 → 强制 eval.py --gate

v1 砍了什么:
  - diff_review LLM 语义层 (审计 1.2): 只留硬规则层接口, v1 不启用
  - dependency_graph / affected_area (审计 1.4): refactor 链不分支,
    task_type 仅记录
  - bug 修复验证 (审计 3.2): 项目零测试, bugfix 强制标 unverified
"""

from __future__ import annotations
import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from . import config
from .snapshot import Snapshot


# validate.py 真实输出的 verdict 白名单 (审计 3.1 + Opus 审查: 补"信息不足" + L1 阻断)
_KNOWN_VERDICTS = {"人工复核", "注意", "信息不足", "阻断"}


# L1 提示护栏: agent 产出里出现危险操作 → 直接 abort
_DANGEROUS_PATTERNS = [
    re.compile(r"rm\s+-rf\s+/"),
    re.compile(r"curl.*\|.*sh"),
    re.compile(r"sudo\s+rm"),
    re.compile(r"chmod\s+777"),
    re.compile(r">\s*/dev/sda"),
    re.compile(r"mkfs\."),
    re.compile(r"dd\s+if="),
    re.compile(r"DROP\s+TABLE", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM", re.IGNORECASE),
]

# L3 人审: diff 里删了 security/auth/permission 相关行 → 需人审
_HUMAN_REVIEW_PATTERNS = [
    re.compile(r"^-[^+]*security", re.MULTILINE),
    re.compile(r"^-[^+]*auth", re.MULTILINE),
    re.compile(r"^-[^+]*permission", re.MULTILINE),
]


@dataclass
class ValidationReport:
    verdict: str = "未知"          # 汇总后: 通过 | 人工复核 | gate失败 | 阻断 | 未知
    action: str = "pass"           # pass | retry | rollback | abort
    validate_verdict: str = ""     # validate.py 原始 verdict
    validate_reason: str = ""
    gate_passed: Optional[bool] = None
    gate_message: str = ""
    human_review_required: bool = False  # L3: 删了安全相关行 → 需人审
    unverified: list = field(default_factory=list)  # 未验证项 (写进 neijinglu)
    evidence: dict = field(default_factory=dict)    # validate 全文, 供打回附给 agent
    turns_used: int = 0


def validate(
    candidate: str,
    gate_required: bool,
    task_type: str,
    changed_files: list,
    snap: Snapshot,
    turn: int,
    max_turns: int,
) -> ValidationReport:
    """主验证入口。执行序: L1护栏 → L2文件gate → L3人审 → L4 validate.py 合并。

    任一步 abort 即短路返回。签名不变 (L1 扫 candidate —— 危险操作出现在
    agent 产出而非任务描述里, candidate 是最接近的可用文本)。
    """
    report = ValidationReport(turns_used=turn)

    # L1 提示护栏: 扫 candidate 里的危险操作
    for pat in _DANGEROUS_PATTERNS:
        if pat.search(candidate):
            report.verdict = "阻断"
            report.action = "abort"
            report.unverified.append(f"L1护栏: 危险操作 ({pat.pattern})")
            return report

    # L2 文件 gate: 双重判定 —— router 说的 gate_required 或 changed_files 真命中引擎文件
    if gate_required or _gate_check_by_files(changed_files):
        g = _run_gate()
        report.gate_passed = g.get("passed")
        report.gate_message = g.get("message", "")
        if not g.get("passed"):
            report.verdict = "gate失败"
            report.action = "rollback" if turn >= max_turns else "retry"
            report.unverified.append(f"gate 失败: {report.gate_message}")
            return report

    # L3 人审: candidate (diff 形态) 里删了 security/auth/permission 行
    for pat in _HUMAN_REVIEW_PATTERNS:
        if pat.search(candidate):
            report.human_review_required = True
            report.unverified.append(f"L3人审: 疑删安全相关行 ({pat.pattern})")
            break

    # L4 validate.py (诚实版, 调 I 层)
    v = _run_validate(candidate)
    report.validate_verdict = v.get("verdict", "未知")
    report.validate_reason = v.get("verdict_reason", "")
    report.evidence = v

    # 任务类型 unverified 标注 (v1 不分支验证链, 只标注)
    _annotate_unverified(report, task_type, changed_files)

    # 合并判定 (最硬者优先): 人审 > 人工复核/信息不足 > 注意/未知
    if report.human_review_required:
        report.verdict = "阻断"
        report.action = "abort"   # 人审不 retry, 直接停给人看
    elif report.validate_verdict == "人工复核":
        report.verdict = "人工复核"
        report.action = "retry" if turn < max_turns else "abort"
    elif report.validate_verdict == "信息不足":
        report.verdict = "信息不足"
        report.action = "retry" if turn < max_turns else "abort"
    elif report.validate_verdict == "注意":
        report.verdict = "通过"
        report.action = "pass"
    else:
        report.verdict = "通过"
        report.action = "pass"
        report.unverified.append(f"validate verdict 非白名单: {report.validate_verdict}")

    return report


# ── validate.py 调用 ──────────────────────────────────────────────────
def _run_validate(candidate: str) -> dict:
    if not config.VALIDATE_SCRIPT.exists():
        return {"verdict": "未知", "verdict_reason": "validate.py 不存在"}
    try:
        proc = subprocess.run(
            ["python3", str(config.VALIDATE_SCRIPT), candidate, "--json"],
            capture_output=True, text=True,
            timeout=config.VALIDATE_TIMEOUT,
        )
        if proc.returncode != 0:
            return {"verdict": "未知", "verdict_reason": f"exit={proc.returncode}"}
        return json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        return {"verdict": "未知", "verdict_reason": "validate 超时"}
    except (json.JSONDecodeError, Exception) as e:  # noqa: BLE001
        return {"verdict": "未知", "verdict_reason": f"解析失败: {e}"}


# ── eval.py --gate 调用 (审计 1a) ─────────────────────────────────────
def _run_gate() -> dict:
    if not config.EVAL_SCRIPT.exists():
        return {"passed": True, "message": "eval.py 不存在, 跳过 gate"}
    try:
        proc = subprocess.run(
            ["python3", str(config.EVAL_SCRIPT), "--gate", "--json"],
            capture_output=True, text=True,
            timeout=config.GATE_TIMEOUT,
        )
        data = json.loads(proc.stdout) if proc.stdout else {}
        gate = data.get("gate", {})
        return {
            "passed": gate.get("passed", True),
            "message": gate.get("message", f"exit={proc.returncode}"),
        }
    except subprocess.TimeoutExpired:
        return {"passed": False, "message": f"gate 超时 {config.GATE_TIMEOUT}s"}
    except Exception as e:  # noqa: BLE001 — gate 挂了保守判失败
        return {"passed": False, "message": f"gate 异常: {e}"}


# ── L2 gate 文件判定 ──────────────────────────────────────────────────
def _gate_check_by_files(changed_files: list) -> bool:
    """changed_files 的 basename 命中引擎核心文件 → 触发 gate。"""
    if not changed_files:
        return False
    for f in changed_files:
        name = f.rsplit("/", 1)[-1]  # basename
        if name in config.GATE_TRIGGER_FILES:
            return True
    return False


# ── unverified 标注 ───────────────────────────────────────────────────
def _annotate_unverified(report: ValidationReport, task_type: str, changed_files: list) -> None:
    """v1 不分支验证链, 但诚实标注哪些没验证 (审计 3.2 / 1.4)。"""
    if task_type == "bugfix":
        # 项目零测试, bug 修复无法验证 (审计 3.2)
        report.unverified.append("bugfix: 修复未验证 (项目零测试, 无复现脚本)")
    if task_type == "refactor":
        # dependency_graph 砍了 (审计 1.4), 影响面未分析
        report.unverified.append("refactor: 变更影响面未分析 (dependency_graph v1 未建)")
    if task_type == "feature":
        # diff_review LLM 层 v1 未启用 (审计 1.2), 只硬规则未实现
        report.unverified.append("feature: 安全审查 (diff_review) v1 未启用")
    if not changed_files:
        report.unverified.append("无变更文件 (可能是查询类任务或 E+ patch 未 apply)")


# ── L5 生命周期钩子 (v2 占位) ─────────────────────────────────────────
def pre_execution_hook(task: str, snap) -> list[str]:
    """执行前钩子, 返回 warning 列表。v2 占位, 默认空。"""
    return []


def post_execution_hook(exec_result, snap) -> list[str]:
    """执行后钩子, 返回 warning 列表。v2 占位, 默认空。"""
    return []
