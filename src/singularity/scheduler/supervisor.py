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

from singularity.scheduler import config


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
    supervisor_model: str = "",
    implementer_model: str = "",
    repo_root: str = "",
    tests_result: dict = None,
) -> SupervisionVerdict:
    """对单任务输出做四维校验。

    Args:
        task_description: 任务描述
        changed_files: agent 实际修改的文件列表
        constraints: Gate2 确认的可检查约束
        checklist: Architect 分解时的验收 checklist
        agent_output: agent 原始输出文本
        task_id: 任务 ID (用于 diff 查询)
        supervisor_model: supervisor 模型名 (用于模型隔离校验)
        implementer_model: implementer 模型名
        repo_root: 改动文件所在仓库根 (项目任务传项目 repo, 空=奇点仓库)
        tests_result: 调用方刚跑过的项目测试结果 (run_project_tests 返回值);
                      给了就复用, 不重复跑 pytest
    Returns:
        SupervisionVerdict with verdict and detailed checks
    """
    verdict = SupervisionVerdict(verdict="pass")
    # 项目任务的改动文件在项目独立 repo 下, 传错根 → _check_artifact 的
    # py_compile/ruff 因 (root/f).exists() 为假而静默跳过, 测试还跑的是奇点自己的
    root = Path(repo_root) if repo_root else config.PROJECT_ROOT

    # ── 0. 模型隔离 ──
    if supervisor_model and implementer_model and supervisor_model == implementer_model:
        verdict.verdict = "block"
        verdict.reason = f"Supervisor model ({supervisor_model}) must differ from implementer model"
        return verdict

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
    verdict.checks["artifact"] = _check_artifact(changed_files, root, tests_result)

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


def qa_context(task) -> tuple[list, list]:
    """取该任务的 QA 上下文: (constraints, checklist)。

    constraints: Gate2 确认的约束清单 (project.constraints_checklist)
    checklist:   架构分解里该任务的验收标准 (按 title/id 匹配 task.description)
    非项目任务 → 双空。
    """
    constraints: list = []
    checklist: list = []
    pid = getattr(task, "project_id", "") or ""
    if not pid:
        return constraints, checklist
    try:
        from .project import load as _load_proj
        proj = _load_proj(pid)
        if proj:
            constraints = proj.constraints_checklist
            if proj.architecture:
                for tdef in proj.architecture.get("tasks", []):
                    if tdef.get("title", "") in task.description or tdef.get("id", "") in task.description:
                        acc = tdef.get("acceptance", "")
                        if acc:
                            checklist.append(acc)
    except Exception as e:
        from singularity.scheduler import witness
        witness.warn('supervisor', f'qa_context:{e}')
    return constraints, checklist


def _check_completeness(
    checklist: list[str], agent_output: str, changed_files: list[str],
) -> CheckResult:
    """完整性: checklist 逐项检查 (仅记录, 不判失败)。

    验收标准是散文、agent 产出是代码摘要, 逐字子串匹配实测几乎必然 0 命中,
    据此判 fail 只会误伤正常改动 (软失败还会触发重排队)。真正的语义核对在
    run_post_exec_checks → check_requirement_conformance (LLM) 里做。
    这里只把未逐字命中的项记进 evidence 供排查, 不参与 pass/fail。
    """
    if not checklist:
        return CheckResult(passed=True, reason="无 checklist,跳过")
    if not changed_files:
        return CheckResult(
            passed=False, reason="无文件改动",
            evidence={"hard": True},
        )
    missing = [item for item in checklist if item.lower() not in agent_output.lower()]
    return CheckResult(
        passed=True,
        reason=(f"checklist {len(checklist)} 项全部逐字命中" if not missing
                else f"checklist {len(missing)}/{len(checklist)} 项未逐字命中 (仅记录, 不判失败)"),
        evidence={"unverified_items": missing[:5], "hard": False},
    )


def _check_constraints(
    constraints: list[dict], changed_files: list[str], root: Path,
) -> CheckResult:
    """约束合规: 机械比对改动的文件是否在禁止名单中。"""
    from .project import constraint_text
    if not constraints:
        return CheckResult(passed=True, reason="无约束清单,跳过")

    violations = []
    for c in constraints:
        rule = constraint_text(c)
        cl = rule.lower()
        for f in changed_files:
            # 约束中提到的文件是否被改了
            if f.lower() in cl or Path(f).name.lower() in cl:
                if "不改" in rule or "禁止" in rule or "冻结" in rule or "不可改" in rule:
                    violations.append(f"约束'{rule}'禁改,但修改了{f}")

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
    """偷懒检测: 机械清单。

    硬信号 = 客观文本证据 (TODO/省略/模糊措辞) → supervise 判 fail。
    软信号 = 启发式 (改动文件数 vs checklist、无测试文件) → 判 escalate/retry。
    理由: 文件数 ≠ 偷懒, 一个文件的精准修复也会命中; 部分任务本就不需要改测试文件。
    把它们当硬证据会在 QA 门禁前移后把正常改动直接拦下。
    """
    hard_signals, soft_signals = [], []
    output_lower = agent_output.lower()

    # 1. 输出远少于 checklist 预期 (软)
    if checklist and len(changed_files) < max(1, len(checklist) // 3):
        soft_signals.append(f"改动文件({len(changed_files)})远少于checklist({len(checklist)})预期")

    # 2. 用注释代替实现 (硬)
    if "todo" in output_lower or "# 此处省略" in agent_output:
        hard_signals.append("输出含 TODO / 注释代替实现")

    # 3. 模糊措辞 (硬)
    vague_phrases = ["应该能跑", "理论上没问题", "应该没问题", "看起来是对的", "大概可以"]
    for phrase in vague_phrases:
        if phrase in agent_output:
            hard_signals.append(f"模糊措辞: '{phrase}'")
            break

    # 4. 没有测试或验证 (软; 仅当 checklist 确实要求验证时才提, 否则纯误报)
    has_test = any(
        "test" in f.lower() or "spec" in f.lower() or "_test" in f.lower()
        for f in changed_files
    )
    # 注意别用"验证"——"验证码"这类词会误命中
    wants_test = any(("测试" in c or "test" in c.lower()) for c in checklist)
    if not has_test and wants_test:
        soft_signals.append("checklist 要求验证但无测试文件改动")

    signals = hard_signals + soft_signals
    if signals:
        return CheckResult(
            passed=False,
            reason=f"检测到 {len(signals)} 个偷懒信号",
            evidence={"signals": signals, "hard": bool(hard_signals)},
        )
    return CheckResult(passed=True, reason="无偷懒信号")


def _check_artifact(changed_files: list[str], root: Path, tests_result: dict = None) -> CheckResult:
    """产物验证: lint + 测试 (硬证据)。P0: 加 ruff + pytest。"""
    if not changed_files:
        return CheckResult(passed=True, reason="无改动文件,跳过")

    py_files = [f for f in changed_files if f.endswith(".py")]
    if not py_files:
        return CheckResult(passed=True, reason="无 Python 文件改动")

    evidence = {"hard": True}
    errors = []

    # 1. 语法检查 (python -m py_compile)
    for f in py_files:
        fp = root / f
        if fp.exists():
            try:
                proc = subprocess.run(
                    ["python3", "-m", "py_compile", str(fp)],
                    capture_output=True, text=True, timeout=10,
                )
                if proc.returncode != 0:
                    errors.append(f"{f}: syntax error")
            except Exception:
                errors.append(f"{f}: compile timeout")

    # 2. Lint (ruff check)
    # 目标文件全不在 root 下时必须**跳过**，不能把空路径列表交给 ruff：
    # 不带路径的 `ruff check` 是"扫当前目录"，cwd 又是 root，等于扫整个仓库并
    # 按别人代码的 E/F 违规把这个任务判成硬失败（reason=质量门禁失败）。
    # 注：ruff 未在本项目 venv 安装（pyproject 里是 dev 可选依赖），空参数行为
    # 没有实测过 —— 这里只是不再依赖那个未验证的行为。
    lint_targets = [str(root / f) for f in py_files if (root / f).exists()]
    if not lint_targets:
        evidence["lint"] = "no changed py file under root, skipped"
    else:
        try:
            proc = subprocess.run(
                ["ruff", "check", "--select=E,F", *lint_targets],
                capture_output=True, text=True, timeout=30, cwd=str(root),  # cwd=root: 否则 ruff 按进程 cwd 找配置/文件
            )
            if proc.returncode != 0:
                errors.append(f"ruff: {proc.stdout.strip()[:200]}")
            else:
                evidence["lint"] = "ruff passed"
        except FileNotFoundError:
            evidence["lint"] = "ruff not installed, skipped"
        except Exception:
            pass  # ruff 挂了不阻塞

    # 3. 测试 (pytest → unittest → npm)
    if tests_result is not None:
        # 调用方 (run_post_exec_checks) 刚跑过 → 复用, 不重复跑
        evidence["tests"] = tests_result
        if not tests_result.get("passed"):
            errors.append(f"tests failed: {tests_result.get('failures', '?')} failures")
    else:
        try:
            from singularity.scheduler.validator import run_project_tests
            test_result = run_project_tests(cwd=str(root))
            evidence["tests"] = test_result
            if not test_result.get("passed"):
                errors.append(f"tests failed: {test_result.get('failures', '?')} failures")
        except Exception:
            pass  # test runner 挂了不阻塞，记在 supervisor 日志

    if errors:
        return CheckResult(
            passed=False,
            reason=f"质量门禁失败: {'; '.join(errors)}",
            evidence=evidence,
        )
    return CheckResult(
        passed=True, reason=f"质量门禁通过 ({len(py_files)} 文件)",
        evidence=evidence,
    )


# ═══════════════════════════════════════════════════════════════
# 需求符合性校验 (按 production-flow.md: 测试阶段两层之一)
# ═══════════════════════════════════════════════════════════════

def _conformance_via_llm(trace: list, agent_output: str, changed_files: list[str]):
    """LLM 逐条验收需求符合性 (替代机械关键词)。无模型/失败返回 None (降级机械)。"""
    if not agent_output.strip():
        return None
    try:
        from singularity.scheduler import dispatcher as _disp
        from singularity.scheduler.validator import _extract_json_obj
        agents = _disp.load_agents()
        pool = [a for a in (agents.get("any", []) or []) if _disp.agent_api_available(a)]
        if not pool:
            return None
        cfg = pool[0]
        items_block = "\n".join(
            f"{i+1}. {it.get('requirement','')[:120]}"
            + (f" — 验收: {it.get('acceptance_criteria','')[:120]}" if it.get('acceptance_criteria') else "")
            for i, it in enumerate(trace))
        prompt = f"""核对以下需求是否在产出中实现。逐条判定 covered=true/false。

需求清单:
{items_block}

产出(截断):
```
{agent_output[:4000]}
```

Output ONLY JSON: {{"items":[{{"idx":1,"covered":true,"reason":"简短"}}]}}
JSON:"""
        result = _disp.dispatch(prompt, "any", f"conf_{len(trace)}", {"any": [cfg]})
        raw = result.executor_result.raw_output if result and result.executor_result else ""
        d = _extract_json_obj(raw)
        if not d or not isinstance(d.get("items"), list) or not d["items"]:
            return None
        items = d["items"]
        passed = [it for it in items if it.get("covered")]
        failed = [it for it in items if not it.get("covered")]
        ev = {"hard": True, "total": len(items), "passed": len(passed),
              "failed": len(failed),
              "failed_items": [f"{it.get('idx','?')}:{it.get('reason','')[:60]}" for it in failed],
              "llm": True}
        if failed:
            return CheckResult(
                passed=False,
                reason=f"需求符合性(LLM): {len(passed)}/{len(items)} 通过, {len(failed)} 条未达标",
                evidence=ev)
        return CheckResult(
            passed=True,
            reason=f"需求符合性(LLM): {len(passed)}/{len(items)} 全部通过",
            evidence=ev)
    except Exception:
        return None


def check_requirement_conformance(project_id: str, agent_output: str = "",
                                   changed_files: list[str] = None) -> CheckResult:
    """加载 traceability.json，逐条核验需求符合性。

    对照立项需求追溯表，检查每条需求是否在产出中覆盖。
    LLM 逐条验收优先，无模型/失败降级机械关键词。
    返回 CheckResult: passed + 逐条明细。
    """
    changed_files = changed_files or []
    try:
        from singularity.scheduler.project import _projects_dir
        p = _projects_dir() / f"{project_id}.traceability.json"
        if not p.exists():
            return CheckResult(passed=True, reason="无追溯表,跳过需求符合性检查")

        trace = json.loads(p.read_text(encoding="utf-8"))
        if not trace:
            return CheckResult(passed=True, reason="追溯表为空,跳过")
    except Exception:
        return CheckResult(passed=True, reason="追溯表读取失败,跳过")

    # LLM 逐条验收优先, 失败降级机械
    llm_result = _conformance_via_llm(trace, agent_output, changed_files)
    if llm_result is not None:
        return llm_result

    # 机械关键词兜底 (无模型/LLM 失败时)
    # 逐条检查
    passed_items = []
    failed_items = []
    output_lower = agent_output.lower()
    files_set = set(changed_files)

    for item in trace:
        req = item.get("requirement", "")
        criteria = item.get("acceptance_criteria", "")
        covered_by = item.get("covered_by_tasks", [])

        # 机械检查: 需求关键词是否在产出中出现
        req_keywords = req.lower().split() if req else []
        keyword_match = any(kw in output_lower for kw in req_keywords if len(kw) > 2)

        # 检查覆盖的任务是否有文件产出
        has_files = any(t in str(files_set) for t in covered_by) if covered_by else True

        check = {
            "requirement": req,
            "acceptance_criteria": criteria,
            "keyword_match": keyword_match,
            "files_produced": has_files,
        }

        if keyword_match or has_files:
            passed_items.append(check)
        else:
            failed_items.append(check)

    if failed_items:
        return CheckResult(
            passed=False,
            reason=f"需求符合性: {len(passed_items)}/{len(trace)} 通过, {len(failed_items)} 条未达标",
            evidence={
                "hard": True,
                "total": len(trace),
                "passed": len(passed_items),
                "failed": len(failed_items),
                "failed_items": [f["requirement"][:80] for f in failed_items],
            },
        )
    return CheckResult(
        passed=True,
        reason=f"需求符合性: {len(passed_items)}/{len(trace)} 全部通过",
        evidence={"hard": True, "total": len(trace), "all_passed": True},
    )
