"""_review.py — post-execution checks: project tests + multi-model review.

Extracted from _exec.py to keep the execution engine focused on dispatch flow.

D1: 审查失败上限 (auto-fix max 2轮) + 超时一律判FAIL + 安全项标记不放行。
"""

from __future__ import annotations
import os
import subprocess
import time
from pathlib import Path

from singularity.scheduler import witness

# D1: 审查自动修上限 (比实现层3轮更紧, 审查修不动=架构/拆解有问题)
_REVIEW_MAX_AUTO_FIX = 2
# D1: 审查超时阈值 (秒)
_REVIEW_TIMEOUT_SEC = 600  # 10 minutes


def _norm(v) -> str:
    """LLM 返回的枚举值（severity / status…）归一化后再比：小写 + 去空白。

    审查判官是模型，输出会在大小写/空格上飘（"Critical"、"Fail "、"HIGH"）。
    直接拿字面量精确匹配的话，一个字母的差别就能让 critical/fail 匹配不上 →
    被当成软信号**放行** —— 正是这个仓库修过一轮的 fail-open。
    缺失/None 归一成空串；空串不匹配任何硬拦档，等价于"没给这个字段"。
    """
    # 不能写 d.get(k, "")：键存在但值为 None 时默认值不生效，
    # str(None) 会得到 "none" 这种凭空的档位。
    return "" if v is None else str(v).strip().lower()


def _sev(item) -> str:
    """LLM 返回的 severity，归一化后比。见 _norm。"""
    return _norm((item or {}).get("severity"))


def _expand_review_pool(disp_mod, writer_model: str, chosen: list[str],
                        want: int = 2) -> list[str]:
    """启用的 agent 凑不齐 reviewer 时，从**模型注册表**补人。

    为什么需要：2 模型阵容里排除 writer 就只剩 1 个，"多模型审查"名不副实，
    而多视角碰撞正是核心价值主张。原先是只告警不补（warn:single_reviewer）。

    扩池会调用**未启用**的模型 —— 是真花钱，所以三条约束：
      ① 只在少到名不副实时才补（调用方判 len < want），不会把每次审查都变成全池
      ② 补够 want 就停
      ③ 每次补都告警（花钱的事必须留痕）
    QIDIAN_REVIEW_POOL_EXPAND=0 可整体关掉。
    """
    if os.environ.get("QIDIAN_REVIEW_POOL_EXPAND", "1") == "0":
        return []
    try:
        from . import model_registry as mr
        skip = {writer_model} | set(chosen)
        out: list[str] = []
        for mid in mr.load_models():
            if mid in skip:
                continue
            cfg = {"model": mid}
            # agent_api_available 会**就地**补全 type/provider/api_key_env；
            # 补不出 type 说明注册表里没有这个模型的可用配置（或 provider 没配 key）。
            # 它内部还有一道 OpenAI 硬限制（不在 _order 里就不放行），别绕过。
            if disp_mod.agent_api_available(cfg) and cfg.get("type"):
                out.append(mid)
                if len(chosen) + len(out) >= want:
                    break
        return out
    except Exception as e:
        # 扩池失败不该阻断审查 —— 退回单 reviewer，但要留痕
        witness.warn("review", f"review_pool_expand_failed:{type(e).__name__}"[:80])
        return []


def _is_trivial_change(changed: list[str], cwd: str) -> bool:
    """单文件且 diff < 50 行 → 跳过审查。"""
    if len(changed) != 1:
        return False
    try:
        r = subprocess.run(["git", "diff", changed[0]],
                         capture_output=True, text=True, timeout=10, cwd=cwd)
        line_count = len([l for l in (r.stdout or "").split("\n") if l])
        return line_count < 50
    except Exception:
        return False


def _review_requirements(task) -> str:
    """审查用需求: 顶层需求 + 约束 + 本任务验收 + 本任务描述 (顶层优先, 供逐条核对)."""
    parts = []
    pid = getattr(task, 'project_id', '')
    proj = None
    if pid:
        try:
            from .project import load as _load_proj, constraint_text
            proj = _load_proj(pid)
        except Exception:
            proj = None
    if proj:
        if getattr(proj, 'description', ''):
            parts.append(f"[顶层需求] {proj.description[:300]}")
        if proj.constraints_checklist:
            parts.append(f"[约束] {'; '.join(constraint_text(c) for c in proj.constraints_checklist[:5])}")
        if proj.architecture:
            desc = getattr(task, 'description', '')
            for tdef in proj.architecture.get("tasks", []):
                if tdef.get("title", "") in desc or tdef.get("id", "") in desc:
                    acc = tdef.get("acceptance", "")
                    if acc:
                        parts.append(f"[验收标准] {acc}")
                    break
    parts.append(f"[本任务] {task.description or ''}")
    return "\n".join(parts)


def run_post_exec_checks(*, validation, quality, exec_result,
                          task, agent_cfg, level, cwd, changed) -> None:
    """Run project tests + multi-model review after agent execution.

    Mutates validation and quality dicts in place.

    D1: 审查超时/失败上限 — 累计自动修 >= _REVIEW_MAX_AUTO_FIX → 升GATE2兜底。
    S2: 超时检测改为真实 (用 ThreadPoolExecutor 带 timeout 包装耗时操作)。
    S3: reviewer_models 用 _all_agents_list 取全池, 去重复分支。
    """
    from . import dispatcher as disp_mod
    from . import validator as val_mod
    import concurrent.futures

    start_time = time.time()
    project_id = getattr(task, 'project_id', '')

    def _record_review_failure(reason: str):
        """B6: 审查失败计数 + 触顶检查, 写回 project.review_failures。"""
        if not project_id:
            return
        try:
            from . import project as proj_mod
            proj = proj_mod.load(project_id)
            if proj is None:
                return
            proj.review_failures = getattr(proj, 'review_failures', 0) + 1
            proj_mod.save(proj)
            fail_check = check_review_fail_limit(project_id, proj.review_failures)
            if fail_check["blocked"]:
                quality["warnings"].append(fail_check["reason"])
                quality["failure_kind"] = "review_limit_hit"
        except Exception as e:
            # 不能静默: 计数写不进去 = 触顶兜底永不生效 (退化成无限自动重试)
            try:
                from . import witness
                witness.warn('review', f'record_review_failure:{e}')
            except Exception:
                pass

    # 0) 本地安全扫描 (正则筛危险代码, 零成本前置防线)
    if changed and cwd:
        try:
            _sec = []
            for _f in changed[:20]:
                _p = Path(cwd) / _f
                if not _p.exists():
                    continue
                _r = val_mod.security_review(_p.read_text(encoding="utf-8", errors="ignore"),
                                             file_path=_f, severity_filter="critical")
                _sec.extend(_r.get("issues", []))
            if _sec:
                quality["warnings"].append(
                    f"安全扫描发现 {len(_sec)} 处危险代码: " +
                    "; ".join(i["detail"][:60] for i in _sec[:3]))
                quality["failure_kind"] = "security"
                quality["confidence"] = max(0.0, quality.get("confidence", 0.5) - 0.3)
                validation.action = "retry"
                validation.unverified.append(f"安全扫描: {len(_sec)} 处危险模式")
        except Exception:
            pass

    # 本次改动是否被判为"小改动"（单文件 + diff<50 行）。
    # ⚠️ 2026-09-11 审计 P0-1 已知缺陷：worktree 里改动在 validate **之前**已被 commit_wt
    # 提交，_is_trivial_change 里的裸 `git diff` 恒为 0 行 → **单文件改动恒判 trivial**。
    # 本次只做披露、不改判据（多模型审查开销大，是否全开另行决定）。
    _trivial = bool(changed) and _is_trivial_change(changed, cwd)
    if validation.action == "pass" and changed and _trivial:
        validation.unverified.append(
            "审查已跳过: 改动被判为小改动(单文件, diff<50行) — 未跑项目测试/未多模型审查")

    # 1) run project tests (S2: 带超时包装)
    # 小改动(单文件<50行)跳过项目全量测试：独立小任务(如写 hello.py)跟项目测试套件无关，跑了会误判
    if validation.action == "pass" and changed and not _trivial:
        try:
            # 不能用 `with ThreadPoolExecutor(...)`: 退出时会 shutdown(wait=True) 去 join，
            # 底层调用挂死的话超时形同虚设 —— 整个任务跟着挂（实测 A/B 探针三次这样卡住）。
            # 显式 shutdown(wait=False): 放弃等待，让流水线能继续/能收尾。
            _ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                fut = _ex.submit(val_mod.run_project_tests, cwd=cwd)
                try:
                    test_result = fut.result(timeout=_REVIEW_TIMEOUT_SEC)
                # 必须是 TimeoutError，**不是 TimeoutExpired** —— 后者在 concurrent.futures
                # 命名空间里根本不存在（Python 3.14 连 _base.TimeoutExpired 都没了）。
                # 写成 TimeoutExpired 时求值异常类会抛 AttributeError，被外层 except 接走：
                # 这条分支永远进不来，`return`（不再往下跑更贵的步骤）也跟着失效。
                except concurrent.futures.TimeoutError:
                    quality["warnings"].append(f"审查超时(>{_REVIEW_TIMEOUT_SEC}s) — 不默认通过, 升GATE2兜底")
                    quality["failure_kind"] = "review_timeout"
                    quality["confidence"] = max(0.0, quality.get("confidence", 0.5) - 0.4)
                    validation.action = "retry"
                    validation.unverified.append("测试执行超时: 不默认通过, 需人工兜底")
                    _record_review_failure("test_timeout")
                    return
                quality["test_result"] = test_result  # 供 supervisor._check_artifact 复用, 免重复跑
                if not test_result.get("passed"):
                    quality["warnings"].append(
                        f"tests failed ({test_result.get('runner','?')}): "
                        f"{test_result.get('failures','?')} failures")
                    quality["failure_kind"] = "test_failure"
                    quality["confidence"] = max(0.0, quality.get("confidence", 0.5) - 0.3)
                    validation.unverified.append(
                        f"tests failed: {test_result.get('output','')[:200]}")
                    validation.action = "retry"
                    _record_review_failure("test_failure")
                elif test_result.get("runner") != "none":
                    quality["quality_signals"]["tests_passed"] = test_result.get("total", 0)
                    quality["confidence"] = min(1.0, quality.get("confidence", 0.5) + 0.1)
                else:
                    # runner == "none" = pytest/unittest/npm 三个全不可用或全超时，
                    # 返回的 passed 仍是初值 True。这是**没跑**，不是**跑过了**。
                    # 不加分也不拦（test_validator.test_run_tests_no_tests 锁定了那个语义），
                    # 但必须披露 —— 否则交付报告把"没验证"和"验证通过"混为一谈。
                    validation.unverified.append(
                        "项目测试未执行: 无可用 runner (pytest/unittest/npm 均不可用)")
            finally:
                _ex.shutdown(wait=False)   # 不 join：挂死的调用不能拖住整条流水线
        except Exception as e:
            quality["warnings"].append(f"test execution error: {e}")
            quality["failure_kind"] = "test_error"
            quality["confidence"] = max(0.0, quality.get("confidence", 0.5) - 0.3)
            validation.action = "retry"
            validation.unverified.append("测试执行异常: 不默认通过")
            _record_review_failure("test_error")

    # 2) multi-model review: 2+ models independently review changed files
    # ponytail: 小改动跳过审查 — 单文件 + <50行diff 不值得额外90s开销
    if validation.action == "pass" and changed and not _trivial:
        try:
            writer_model = agent_cfg.get("model", "")
            agents_all = disp_mod.load_agents()
            # S3: 用 _all_agents_list 一次取全池, 去重复分支
            all_pool = disp_mod._all_agents_list(agents_all)
            reviewer_models = [
                a['model'] for a in all_pool
                if a['model'] != writer_model and disp_mod.agent_api_available(a)][:2]

            if len(reviewer_models) < 2:
                # 只有 2 个启用的 agent 时，排除 writer 就只剩 1 个 —— "多模型审查"
                # 名不副实，而"多视角碰撞"正是核心价值主张。先从注册表补人；
                # 补不到才退化成单 reviewer（并留痕）。
                extra = _expand_review_pool(disp_mod, writer_model, reviewer_models)
                if extra:
                    witness.warn("review",
                                 f"review_pool_expanded:{'+'.join(extra)}"[:120])
                    reviewer_models += extra
                else:
                    witness.warn("review",
                                 f"single_reviewer:{writer_model}:pool={len(all_pool)}"[:80])
            if reviewer_models:
                rev_files = []; rev_models = []; all_issues = []
                review_failed = False
                if len(changed) > 3:
                    # 只审前 3 个文件 —— 改得多时后面的没人看（成本控制：每个文件都要多模型
                    # 审查一轮）。这条截断是**有意为之**，不是 bug。
                    # 但必须**同时**记进 unverified：本模块的原则是"可以放行，但不把通过和
                    # 已验证混为一谈"（见文件头）。只发告警的话，交付报告仍写 delivered，
                    # 而 1/4 的改动没有任何人看过 —— 告警是给排障的人看的，报告是给用户看的。
                    witness.warn("review",
                                 f"review_files_truncated:{len(changed)}->3"[:80])
                    validation.unverified.append(
                        f"{len(changed) - 3} 个改动文件未审查（只审了前 3 个）: "
                        + ", ".join(str(f) for f in changed[3:]))
                for f in changed[:3]:
                    # S2: 多模型审查带超时
                    try:
                        # 同上面那条：不能 `with`（退出 join 会把挂死调用拖成永久阻塞）
                        _ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                        try:
                            fut = _ex.submit(val_mod.multi_model_review,
                                filepath=f, models=reviewer_models, cwd=cwd, diff_only=True,
                                requirements=_review_requirements(task))
                            review = fut.result(timeout=_REVIEW_TIMEOUT_SEC)
                        finally:
                            _ex.shutdown(wait=False)
                    except concurrent.futures.TimeoutError:   # 不是 TimeoutExpired，见上面注释
                        quality["warnings"].append("多模型审查超时 — 不默认通过")
                        quality["failure_kind"] = "review_timeout"
                        quality["confidence"] = max(0.0, quality.get("confidence", 0.5) - 0.4)
                        validation.action = "retry"
                        validation.unverified.append("多模型审查超时: 不进入验收")
                        _record_review_failure("multi_review_timeout")
                        return
                    rev_files.append(f)
                    rev_models = review.get("models_used", [])
                    if not rev_models:
                        # 审查输入为空（见 P0-1）时一个模型都不会被调 —— 这不是
                        # "审过且没问题"，必须如实披露，别让报告写 delivered。
                        validation.unverified.append(
                            f"未经多模型审查: {f} (审查输入为空, 无模型实际参与)")
                    issues = review.get("issues", [])
                    if issues:
                        crit = [i for i in issues if _sev(i) == "critical"]
                        warns = [i for i in issues if _sev(i) == "warning"]
                        if crit:
                            details = "; ".join(
                                f"{i.get('model','')}:{i.get('detail','')[:60]}"
                                for i in crit[:3])
                            quality["warnings"].append(
                                f"multi-review {f}: {len(crit)} critical: {details}")
                            quality["failure_kind"] = "review_critical"
                            quality["confidence"] = max(
                                0.0, quality.get("confidence", 0.5) - 0.25)
                            validation.action = "retry"
                            review_failed = True
                            break
                        elif warns:
                            quality["warnings"].append(
                                f"multi-review {f}: {len(warns)} warnings")
                            quality["confidence"] = max(
                                0.0, quality.get("confidence", 0.5) - 0.1)
                            # 5a: 软质量显式化 (trace/QA 可见)
                            quality["quality_signals"]["soft_warnings"] = \
                                quality["quality_signals"].get("soft_warnings", 0) + len(warns)
                            # 5b: 软质量触发一次软修复 (首轮 retry, 触顶放行见 _decide_cascade)
                            quality["failure_kind"] = "soft_quality"
                            validation.action = "retry"
                    all_issues.extend(issues)
                    if review.get("verdicts"):
                        # prompt 契约（validator.multi_model_review）的枚举是 pass|retry|abort，
                        # 原来比的是 "needs_fix" —— 那个值永远不会出现，分支从不成立：
                        # 多个模型都判 retry/abort 但 issues 为空时，没有任何后果
                        # （不 retry、不 unverified、不记 failure）。按真实枚举改。
                        needs_fix = [v for v in review["verdicts"]
                                     if _norm(v.get("verdict")) in ("retry", "abort", "needs_fix")]
                        if len(needs_fix) >= 2:
                            validation.action = "retry"
                            review_failed = True
                            break
                if review_failed:
                    _record_review_failure("review_critical")
                quality["quality_signals"]["review_models"] = rev_models
                quality["quality_signals"]["review_files"] = rev_files
                quality["quality_signals"]["review_issues"] = len(all_issues)
            else:
                # fallback: single-model crossover review
                review = val_mod.crossover_review(
                    task_desc=_review_requirements(task),
                    raw_output=exec_result.raw_output,
                    changed_files=changed, writer_level=level,
                    writer_model=writer_model, cwd=cwd)
                if review.get("issues"):
                    crit = [i for i in review["issues"]
                            if _sev(i) == "critical"]
                    warns = [i for i in review["issues"]
                             if _sev(i) == "warning"]
                    if crit:
                        quality["warnings"].append(
                            f"review found {len(crit)} critical issues: " +
                            "; ".join(i.get("detail", "")[:60] for i in crit))
                        quality["failure_kind"] = "review_critical"
                        quality["confidence"] = max(
                            0.0, quality.get("confidence", 0.5) - 0.25)
                        validation.action = "retry"
                        _record_review_failure("review_critical")
                    elif warns:
                        quality["warnings"].append(
                            f"review found {len(warns)} warnings")
                        quality["confidence"] = max(
                            0.0, quality.get("confidence", 0.5) - 0.1)
                        quality["quality_signals"]["soft_warnings"] = \
                            quality["quality_signals"].get("soft_warnings", 0) + len(warns)
                        quality["failure_kind"] = "soft_quality"
                        validation.action = "retry"
                if review.get("verdict") == "abort":
                    validation.action = "abort"
                    validation.unverified.append(
                        f"review abort: {review.get('summary','')}")
                quality["quality_signals"]["review_verdict"] = review.get(
                    "verdict", "pass")
                quality["quality_signals"]["review_summary"] = review.get(
                    "summary", "")[:200]
        except Exception as e:
            quality["warnings"].append(f"multi-review error: {e}")
            quality["failure_kind"] = "review_error"
            quality["confidence"] = max(0.0, quality.get("confidence", 0.5) - 0.25)
            validation.action = "retry"
            validation.unverified.append(f"multi-review 异常: {e}")
            _record_review_failure("review_error")

    # 3) QA 约束验收: qa_engineer 角色对照约束清单验证 (补 multi_model_review 不查的约束维度)
    if validation.action == "pass" and changed and not _is_trivial_change(changed, cwd):
        try:
            proj = None
            if project_id:
                from . import project as proj_mod
                proj = proj_mod.load(project_id)
            constraints = getattr(proj, 'constraints_checklist', []) if proj else []
            if constraints:
                diff_text = ""
                try:
                    diff_text = "\n\n".join(
                        subprocess.run(["git", "diff", f],
                                       capture_output=True, text=True, timeout=10, cwd=cwd).stdout
                        for f in changed[:3])
                except Exception as e:
                    quality["warnings"].append(f"QA 验收取 diff 失败: {e}")
                if not diff_text.strip():
                    # 空 diff 时模型看到的是 "(无 diff)"，多半回 accepted —— 那是"没得看"，
                    # 不是"看过了没问题"。必须披露，否则又是一次静默放行。
                    validation.unverified.append(
                        "QA 约束验收看到的 diff 为空: 结论不构成有效验收")
                qa = val_mod.qa_acceptance_review(constraints, diff_text, cwd)
                if _norm(qa.get("verdict")) == "needs_fix":
                    fails = [v for v in qa.get("verifications", [])
                             if _norm(v.get("status")) in ("fail", "warning")]
                    quality["warnings"].append(
                        f"QA 约束验收 {len(fails)} 条未满足: " +
                        "; ".join(v.get("constraint", "")[:40] for v in fails[:3]))
                    quality["failure_kind"] = "constraint_fail"
                    quality["confidence"] = max(0.0, quality.get("confidence", 0.5) - 0.25)
                    validation.action = "retry"
                    _record_review_failure("constraint_fail")
                quality["quality_signals"]["qa_acceptance"] = qa.get("verdict", "unknown")
        except Exception as e:
            quality["warnings"].append(f"QA 约束验收 error: {e}")
            quality["failure_kind"] = "constraint_error"
            quality["confidence"] = max(0.0, quality.get("confidence", 0.5) - 0.25)
            validation.action = "retry"
            validation.unverified.append(f"QA 约束验收异常: {e}")
            _record_review_failure("constraint_error")

    # 3.5) 需求符合性对账: 消费 traceability.json, 只写软信号 + warning (机械关键词, 先不设 hard gate)
    if validation.action == "pass" and project_id and not _is_trivial_change(changed, cwd):
        try:
            from .supervisor import check_requirement_conformance
            conf = check_requirement_conformance(
                project_id, agent_output=getattr(exec_result, 'raw_output', '') or '',
                changed_files=changed)
            if not conf.passed:
                ev = getattr(conf, 'evidence', None) or {}
                quality["warnings"].append(
                    f"需求符合性 {ev.get('passed', 0)}/{ev.get('total', 0)} 通过: "
                    + "; ".join(ev.get("failed_items", [])[:3]))
                quality["quality_signals"]["requirement_conformance"] = ev
            else:
                quality["quality_signals"]["requirement_conformance"] = "passed"
        except Exception as e:
            quality["warnings"].append(f"需求符合性对账 error: {e}")

    # 4) 安全审计: security_auditor 角色 LLM 五维审计 (补正则抓不到的复杂漏洞)
    if validation.action == "pass" and changed and not _is_trivial_change(changed, cwd):
        try:
            diff_text = ""
            try:
                diff_text = "\n\n".join(
                    subprocess.run(["git", "diff", f],
                                   capture_output=True, text=True, timeout=10, cwd=cwd).stdout
                    for f in changed[:3])
            except Exception as e:
                quality["warnings"].append(f"安全审计取 diff 失败: {e}")
            if not diff_text.strip():
                # 空 diff 时模型看到 "(无 diff)"，多半回 clean —— 那是"没得审"，
                # 不是"审过且干净"。安全项更不能混为一谈，必须披露。
                validation.unverified.append(
                    "安全审计看到的 diff 为空: 结论不构成有效审计")
            sa = val_mod.security_audit_review(diff_text, cwd)
            findings = sa.get("findings", []) if _norm(sa.get("verdict")) == "needs_fix" else []
            if findings:
                # 阈值: 仅 critical/high 硬拦 (真漏洞); medium/low = 加固建议/设计不完整, 软信号不硬拦
                hard = [f for f in findings if _sev(f) in ("critical", "high")]
                quality["warnings"].append(
                    f"安全审计 {len(findings)} 条问题: " +
                    "; ".join(f"{f.get('severity','?')}:{f.get('description','')[:40]}"
                              for f in findings[:3]))
                if hard:
                    quality["failure_kind"] = "security_findings"
                    quality["confidence"] = max(0.0, quality.get("confidence", 0.5) - 0.3)
                    validation.action = "retry"
                    _record_review_failure("security_findings")
                else:
                    # 软信号: 加固建议/设计不完整 (不硬拦, 不触发 retry)
                    quality["quality_signals"]["security_soft"] = len(findings)
            quality["quality_signals"]["security_audit"] = sa.get("verdict", "unknown")
        except Exception as e:
            quality["warnings"].append(f"安全审计 error: {e}")
            quality["failure_kind"] = "security_error"
            quality["confidence"] = max(0.0, quality.get("confidence", 0.5) - 0.3)
            validation.action = "retry"
            validation.unverified.append(f"安全审计异常: {e}")
            _record_review_failure("security_error")


def check_review_fail_limit(project_id: str = "", current_retries: int = 0) -> dict:
    """D1: 检查审查失败是否触顶。

    Returns: {"blocked": bool, "action": "continue|escalate_to_gate2", "remaining": int}
    """
    remaining = _REVIEW_MAX_AUTO_FIX - current_retries
    if remaining <= 0:
        return {"blocked": True, "action": "escalate_to_gate2", "remaining": 0,
                "reason": f"审查自动修已达上限({_REVIEW_MAX_AUTO_FIX}轮), 升GATE2人工兜底"}
    return {"blocked": False, "action": "continue", "remaining": remaining}
