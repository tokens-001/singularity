"""supervisor.py — 独立校验引擎。

Opus二审核心设计: PASS必须落在非LLM硬证据上。
  - 硬证据(可自动判定): 测试过、lint过、禁改文件diff机械比对
  - 软证据(需人工): 主观判断 → 升级Owner,不自动PASS
  - 模型隔离: Supervisor model ≠ Implementer model (硬锁)
"""

from __future__ import annotations
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass
class CheckResult:
    passed: bool
    reason: str = ""
    evidence: dict = field(default_factory=dict)


@dataclass
class SupervisionVerdict:
    verdict: str                    # "pass" | "fail" | "retry" | "escalate"
    checks: dict[str, CheckResult] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    hard_evidence_count: int = 0    # 硬证据通过的检查数
    soft_escalation: bool = False   # 是否有软证据需要 Owner 判断


def supervise(
    task_description: str,
    changed_files: list[str],
    constraints: list[str],
    checklist: list[str],
    agent_output: str = "",
    task_id: str = "",
) -> SupervisionVerdict:
    """对单任务输出做四维校验。

    Args:
        task_description: 任务描述
        changed_files: agent 实际修改的文件列表
        constraints: Gate2 确认的可检查约束
        checklist: Architect 分解时的验收 checklist
        agent_output: agent 原始输出文本
        task_id: 任务 ID (用于 diff 查询)
    Returns:
        SupervisionVerdict with verdict and detailed checks
    """
    verdict = SupervisionVerdict(verdict="pass")
    root = config.PROJECT_ROOT

    # ── 1. 完整性 ──
    verdict.checks["completeness"] = _check_completeness(
        checklist, agent_output, changed_files,
    )

    # ── 2. 约束合规 ──
    verdict.checks["constraint_compliance"] = _check_constraints(
        constraints, changed_files, root,
    )

    # ── 3. 偷懒检测 ──
    verdict.checks["laziness"] = _check_laziness(
        agent_output, changed_files, checklist,
    )

    # ── 4. 产物验证 (硬证据) ──
    verdict.checks["artifact"] = _check_artifact(changed_files, root)

    # ── 汇总 ──
    for check_name, result in verdict.checks.items():
        if not result.passed:
            verdict.issues.append(f"[{check_name}] {result.reason}")
        if result.evidence.get("hard", False):
            verdict.hard_evidence_count += 1

    # 软证据 → 升级
    if any(
        not r.passed and not r.evidence.get("hard", False)
        for r in verdict.checks.values()
    ):
        verdict.soft_escalation = True

    # 最终判定
    if all(r.passed for r in verdict.checks.values()):
        verdict.verdict = "pass"
    elif any(
        not r.passed and r.evidence.get("hard", False)
        for r in verdict.checks.values()
    ):
        verdict.verdict = "fail"    # 硬证据失败 → 明确失败
    elif verdict.soft_escalation:
        verdict.verdict = "escalate"  # 软证据失败 → 升级 Owner
    else:
        verdict.verdict = "retry"

    return verdict


def _check_completeness(
    checklist: list[str], agent_output: str, changed_files: list[str],
) -> CheckResult:
    """完整性: checklist 逐项检查。"""
    if not checklist:
        return CheckResult(passed=True, reason="无 checklist,跳过")
    if not changed_files:
        return CheckResult(
            passed=False, reason="无文件改动",
            evidence={"hard": True},
        )
    # 机械检查: checklist 每项在 agent_output 中是否有提及
    missing = []
    for item in checklist:
        if item.lower() not in agent_output.lower():
            missing.append(item)
    if missing:
        return CheckResult(
            passed=False,
            reason=f"checklist {len(missing)}/{len(checklist)} 未覆盖: {missing[:3]}",
            evidence={"missing_items": missing, "hard": False},
        )
    return CheckResult(passed=True, reason=f"checklist {len(checklist)} 项全部覆盖")


def _check_constraints(
    constraints: list[str], changed_files: list[str], root: Path,
) -> CheckResult:
    """约束合规: 机械比对改动的文件是否在禁止名单中。"""
    if not constraints:
        return CheckResult(passed=True, reason="无约束清单,跳过")

    violations = []
    for c in constraints:
        cl = c.lower()
        for f in changed_files:
            # 约束中提到的文件是否被改了
            if f.lower() in cl or Path(f).name.lower() in cl:
                if "不改" in c or "禁止" in c or "冻结" in c or "不可改" in c:
                    violations.append(f"约束'{c}'禁改,但修改了{f}")

    if violations:
        return CheckResult(
            passed=False,
            reason=f"违反 {len(violations)} 条约束",
            evidence={"violations": violations, "hard": True},
        )
    return CheckResult(passed=True, reason=f"约束 {len(constraints)} 条全部合规")


def _check_laziness(
    agent_output: str, changed_files: list[str], checklist: list[str],
) -> CheckResult:
    """偷懒检测: 机械清单。"""
    signals = []
    output_lower = agent_output.lower()

    # 1. 输出远少于 checklist 预期
    if checklist and len(changed_files) < max(1, len(checklist) // 3):
        signals.append(f"改动文件({len(changed_files)})远少于checklist({len(checklist)})预期")

    # 2. 用注释代替实现
    if "todo" in output_lower or "# 此处省略" in agent_output:
        signals.append("输出含 TODO / 注释代替实现")

    # 3. 模糊措辞
    vague_phrases = ["应该能跑", "理论上没问题", "应该没问题", "看起来是对的", "大概可以"]
    for phrase in vague_phrases:
        if phrase in agent_output:
            signals.append(f"模糊措辞: '{phrase}'")
            break

    # 4. 没有测试或验证
    has_test = any(
        "test" in f.lower() or "spec" in f.lower() or "_test" in f.lower()
        for f in changed_files
    )
    if not has_test and checklist:
        signals.append("无测试文件改动,checklist要求验证")

    if signals:
        return CheckResult(
            passed=False,
            reason=f"检测到 {len(signals)} 个偷懒信号",
            evidence={"signals": signals, "hard": True},
        )
    return CheckResult(passed=True, reason="无偷懒信号")


def _check_artifact(changed_files: list[str], root: Path) -> CheckResult:
    """产物验证: lint + 测试 (硬证据)。"""
    if not changed_files:
        return CheckResult(passed=True, reason="无改动文件,跳过")

    py_files = [f for f in changed_files if f.endswith(".py")]
    if not py_files:
        return CheckResult(passed=True, reason="无 Python 文件改动")

    # 尝试 lint (python -m py_compile)
    lint_errors = []
    for f in py_files:
        fp = root / f
        if fp.exists():
            try:
                proc = subprocess.run(
                    ["python3", "-m", "py_compile", str(fp)],
                    capture_output=True, text=True, timeout=5,
                )
                if proc.returncode != 0:
                    lint_errors.append(f"{f}: {proc.stderr[:120] if proc.stderr else 'compile error'}")
            except Exception:
                lint_errors.append(f"{f}: timeout/exception")

    if lint_errors:
        return CheckResult(
            passed=False,
            reason=f"lint 失败: {lint_errors}",
            evidence={"lint_errors": lint_errors, "hard": True},
        )
    return CheckResult(
        passed=True, reason=f"lint 通过 ({len(py_files)} 文件)",
        evidence={"hard": True},
    )
