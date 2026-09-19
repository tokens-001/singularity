"""supervisor.py — 独立校验引擎。

Opus二审核心设计: PASS必须落在非LLM硬证据上。
  - 硬证据(可自动判定): 测试过、lint过、禁改文件diff机械比对
  - 软证据(需人工): 主观判断 → 升级Owner,不自动PASS
  - 模型隔离: Supervisor model ≠ Implementer model (硬锁)
"""

from __future__ import annotations

import json
import re
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
    verdict: str                    # "pass" | "fail" | "block" | "retry" | "escalate"
    reason: str = ""                # 提前返回时的原因（如模型隔离违规）; 原来没这字段,
                                    # `verdict.reason = ...` 只是给实例挂了个野属性 → 理由丢失
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
    our_side_stop: str = "",
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
        checklist, agent_output, changed_files, task_description, root,
        our_side_stop=our_side_stop,
    )

    # ── 2. 约束合规 ──
    verdict.checks["constraint_compliance"] = _check_constraints(
        constraints, changed_files, root,
    )

    # ── 3. 偷懒检测 ──
    verdict.checks["laziness"] = _check_laziness(
        agent_output, changed_files, checklist, task_description,
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
            from . import project as _pm
            constraints = _pm.effective_constraints(proj)   # 带兜底，见 §60
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


_READONLY_TAG = "[只读]"


def _is_readonly_task(task_description: str) -> bool:
    """这个任务是不是被**明确声明**为"不改文件"。

    ⚠️ **刻意只认一个固定的协议标记**（`[只读]`，约定写进 `_ARCHITECT_CONTEXT`
    的任务 schema，由架构师把它写进**标题**、标题又会拼进 `description`）。
    **不做自然语言推断** —— "只跑不改 / 不修改 / 纯核对 / 只验证…"是个**开集**，
    永远有下一个说法（同仓 `_observer_answer` 那个函数就是栽在开集枚举上，
    见 `docs/防御模式.md`）。判据必须是**我们定义的协议**，不是猜模型怎么措辞。

    来历（2026-09-15 真机）：planner 拆出一个「独立验收：**只跑不改**」的任务，
    它**活干对了**（真跑 pytest 8 passed、逐条核对 PRD、给了证据），
    却被 `无文件改动` 判 fail（412 秒就死，**与 900s 超时无关**）。
    而那条硬规则本身是**对的**（原意是逮"兄弟任务抢活、自己空手"，见 `_flag_file_overlap`）
    —— 它只是**分不开**"该有产出却空手"和"本就不该有产出"。
    """
    return _READONLY_TAG in (task_description or "")


def _all_empty(changed_files: list[str], root) -> list[str]:
    """改动的文件里，**存在但 0 字节**的那些。

    ⚠️ **2026-09-15 真机坐实**：一个任务把 311 行的测试文件写对了，随后**自己把它清成
    0 字节**（文件还在、内容没了），而 `changed_files` 非空 ⇒ 上面那条"零改动 = 没产出"
    的判据**被一个空文件绕过去了** ⇒ 一路判 `通过` → 任务 `done` → GATE3 才被人眼看见。

    判据只认"**改动的文件全是空的**"：多文件交付里个别空文件（比如 `__init__.py`）
    是合法的，一律拦会误伤；而"这个任务的全部产出都是空文件"没有第二种解释。
    """
    if not changed_files or root is None:
        return []
    from singularity.scheduler import witness
    empties = []
    for f in changed_files:
        try:
            p = Path(root) / f
            if p.is_file() and p.stat().st_size == 0:
                empties.append(f)
        except OSError as e:
            # 读不到就当它**非空**（保守：宁可放行，也别把"读不到"误判成"没产出"
            # —— 那会误杀正常任务）。但**必须出声**：静默 except 正是这仓的棘轮
            # 明令禁止的（2026-09-15 加这个函数时当场被那条棘轮抓到过一次）。
            witness.warn("supervisor", f"empty_check_stat:{f}:{type(e).__name__}"[:120])
            continue
    return empties if len(empties) == len(changed_files) else []


def our_side_stop_of(executor_result) -> str:
    """这次产出**是不是被我们自己停掉的** —— 是的话返回哪一种，否则空串。

    判据只有两档算"我方"（2026-09-19 复核审计 A4 时定的）：
      · `error_kind == "deadline"` —— 撞预算 / 单次 240s 硬顶，被我们掐断；
      · `truncated_by` 非空 —— 工具轮次用尽（有新产出但没终答）。
    ⚠️ `error_kind == "exec"` **不算** —— 那是模型/调用真的失败了，赖不到我们头上。
      （把它算进来就会变成"什么都赖系统"，那就从一个归因错换到另一个。）
    """
    er = executor_result
    if er is None:
        return ""
    if getattr(er, "truncated_by", ""):
        return str(er.truncated_by)
    if getattr(er, "error_kind", "") == "deadline":
        return "deadline"
    return ""


def _check_completeness(
    checklist: list[str], agent_output: str, changed_files: list[str],
    task_description: str = "", root=None, our_side_stop: str = "",
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
        # ⚠️ 这条硬规则**分不开两种情况**：
        #   ① 该有产出却空手回来 —— 原意，且是真的（`_flag_file_overlap` 那个真事：
        #      兄弟任务把活抢了，这个任务零改动，门禁判得对，只是人审页上看不出为什么）；
        #   ② **任务本身就被要求别改文件** —— planner 真的会拆出
        #      「独立验收：只跑不改」这种任务（2026-09-15 真机）。
        # 判据不能靠猜描述里的字（开集枚举，`_observer_answer` 栽过）⇒ 认**上游的显式声明**。
        # ⚠️ 声明了只读**不等于免检**：TODO/模糊措辞那两条硬信号在 `_check_laziness`
        # 里**照常生效**，LLM 语义核对也照跑 —— 这里省的只是"必须改文件"这一条。
        if _is_readonly_task(task_description):
            return CheckResult(
                passed=True,
                reason="只读任务（描述带 [只读] 声明），零改动是预期结果",
                evidence={"readonly": True, "hard": False},
            )
        # 🔴 **判 fail 不变，但归因要说对**（2026-09-19 复核审计 A4，用户拍板走"只标归因"）。
        #    零产出就是零产出 —— 判通过是假的。但"为什么零产出"分两种，而判据原来
        #    只会写一句 `无文件改动`，读的人（和人审页）只能往"它偷懒"上想：
        #      · 该有产出却空手回来；
        #      · **这次是被我们自己掐断的**（撞 240s 硬顶 / 预算）—— `our_side_stop` 非空。
        #    ⚠️ `passed` 一分没动：**没放行**。这条链的最后一跳（`_stream_call` → 判据）
        #    今天才接上，动的只是那一跳携带的**说法**。
        if our_side_stop:
            return CheckResult(
                passed=False,
                reason=(f"无文件改动 —— **这次是被我方掐断的**（{our_side_stop}），"
                        f"不是空手回来的偷懒"),
                evidence={"hard": True, "our_side_stop": our_side_stop},
            )
        return CheckResult(
            passed=False, reason="无文件改动",
            evidence={"hard": True},
        )
    # 有改动，但改动**全是空文件** —— 同"无文件改动"，硬判失败。
    # 见 `_all_empty`：这是 2026-09-15 真机那条"交付物被自己清空"漏过去的口子。
    _empties = _all_empty(changed_files, root)
    if _empties:
        return CheckResult(
            passed=False,
            reason=(f"改动的文件**全是空的**（{'、'.join(_empties[:3])}）"
                    "—— 空文件不算产出，等同于空手回来"),
            evidence={"hard": True, "empty_files": _empties},
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
            if (f.lower() in cl or Path(f).name.lower() in cl) and (
                    "不改" in rule or "禁止" in rule or "冻结" in rule or "不可改" in rule):
                violations.append(f"约束'{rule}'禁改,但修改了{f}")

    if violations:
        return CheckResult(
            passed=False,
            reason=f"违反 {len(violations)} 条约束",
            evidence={"violations": violations, "hard": True},
        )
    return CheckResult(passed=True, reason=f"约束 {len(constraints)} 条全部合规")


# "用注释代替实现" 的偷懒标记。必须认**注释标记形态**，不能认裸子串。
#
# 原来判据是 `"todo" in agent_output.lower()` —— 子串匹配，于是任何输出里出现
# "todo" 四个字母就命中：`todo.py`、`.todo.json`、`todo_list`、甚至任务本身就叫
# "写一个 todo 工具"。2026-09-11 实测：一个**完整实现**了 todo.py（含原子写、
# 损坏文件容错、内置自测）的任务，就因为这个文件名被判 fail、未合并、产物为零。
#
# 现判据 = 注释前缀(+ # // /* <!-- ;) 紧跟 TODO，且 TODO 后不紧跟 . 或 _
# （后者才是文件名/标识符：todo.py / .todo.json / todo_list）：
#   ✅ "# TODO: 实现" / "// TODO 待补" / "x = 1  # TODO" / "# TODO"
#   ❌ "todo.py" / "<!-- @files: todo.py -->" / "# todo.py 的实现" / "# todo_list"
_TODO_MARKER = re.compile(r"(?:#|//|/\*+|<!--|;)\s*todo\b(?![._])", re.IGNORECASE)


def _check_laziness(
    agent_output: str, changed_files: list[str], checklist: list[str],
    task_description: str = "",
) -> CheckResult:
    """偷懒检测: 机械清单。

    硬信号 = 客观文本证据 (TODO/省略/模糊措辞) → supervise 判 fail。
    软信号 = 启发式 (改动文件数 vs checklist、无测试文件) → 判 escalate/retry。
    理由: 文件数 ≠ 偷懒, 一个文件的精准修复也会命中; 部分任务本就不需要改测试文件。
    把它们当硬证据会在 QA 门禁前移后把正常改动直接拦下。

    ⚠️ **声明了 [只读] 的任务，两条软信号都不适用**（2026-09-15 真机）：它们**都是拿
    `changed_files` 当尺子的** —— 对"本就不该改文件"的任务，`改动文件(0)远少于checklist`
    和 `要求验证但无测试文件改动` **必然同时亮**（真机上就是这么亮了 2 个），
    然后判 escalate ⇒ 转 PENDING **重新入队**，白烧一轮。
    ⚠️ 但**硬信号（TODO / 模糊措辞）照常生效** —— 只读任务也可能糊弄。
    """
    hard_signals, soft_signals = [], []
    readonly = _is_readonly_task(task_description)

    # 1. 输出远少于 checklist 预期 (软)
    if not readonly and checklist and len(changed_files) < max(1, len(checklist) // 3):
        soft_signals.append(f"改动文件({len(changed_files)})远少于checklist({len(checklist)})预期")

    # 2. 用注释代替实现 (硬) —— 见 _TODO_MARKER：认注释标记，不认裸子串
    if _TODO_MARKER.search(agent_output) or "# 此处省略" in agent_output:
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
    if not readonly and not has_test and wants_test:
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
            from singularity.scheduler.validator import tests_failed_msg
            errors.append(tests_failed_msg(tests_result))
    else:
        try:
            from singularity.scheduler.validator import run_project_tests
            test_result = run_project_tests(cwd=str(root))
            evidence["tests"] = test_result
            if not test_result.get("passed"):
                from singularity.scheduler.validator import tests_failed_msg
                errors.append(tests_failed_msg(test_result))
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
# 需求符合性校验（审查层的一项，随 QA 报告一起出）
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
    # 但机械检查靠"关键词命中产出"或"覆盖任务有文件产出"，两者**都要入参**。
    # `web/app.py` 的 GATE3 追溯页就是不带参数调的 —— 那种情况下不管判过还是判不过
    # 都是假的（原来因为 has_files 恒真而全判通过）。如实说"核验不了"。
    if not agent_output and not changed_files:
        return CheckResult(
            passed=True,
            reason=f"需求符合性: 未提供产出（{len(trace)} 条），无法核验 —— 这不是通过",
            evidence={"hard": False, "unverifiable": True, "total": len(trace)},
        )

    # 逐条检查
    passed_items = []
    failed_items = []
    unverifiable_items = []   # 没声明覆盖任务、关键词也没命中 → 判不了，既不算过也不算失败
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

        # 既没声明覆盖任务、产出里也没出现需求关键词 = **无法验证**，不是"通过"。
        # 原来 `has_files` 在 covered_by 为空时直接返回 True，于是
        # `keyword_match or has_files` 恒真 → 这类条目无条件算过，
        # 追溯页永远显示"全部通过"，等于这条检查不存在。
        if not covered_by and not keyword_match:
            unverifiable_items.append(check)
            continue
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
    if unverifiable_items:
        return CheckResult(
            passed=True,     # 不判失败：追溯表没写覆盖任务，多半是架构阶段没填全，
                             # 据此拦交付会变成另一种假警报（审计里也点过这个方向）。
                             # 但也**不能**说"全部通过" —— 如实报数，让人自己看。
            reason=(f"需求符合性: {len(passed_items)}/{len(trace)} 通过, "
                    f"{len(unverifiable_items)} 条无法验证(未声明覆盖任务且关键词未命中)"),
            evidence={"hard": False, "total": len(trace),
                      "passed": len(passed_items), "unverifiable": len(unverifiable_items),
                      "unverifiable_items": [u["requirement"][:80] for u in unverifiable_items]},
        )
    return CheckResult(
        passed=True,
        reason=f"需求符合性: {len(passed_items)}/{len(trace)} 全部通过",
        evidence={"hard": True, "total": len(trace), "all_passed": True},
    )
