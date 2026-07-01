"""_review.py — post-execution checks: project tests + multi-model review.

Extracted from _exec.py to keep the execution engine focused on dispatch flow.

D1: 审查失败上限 (auto-fix max 2轮) + 超时一律判FAIL + 安全项标记不放行。
"""

from __future__ import annotations
import subprocess
import time
from pathlib import Path

# D1: 审查自动修上限 (比实现层3轮更紧, 审查修不动=架构/拆解有问题)
_REVIEW_MAX_AUTO_FIX = 2
# D1: 审查超时阈值 (秒)
_REVIEW_TIMEOUT_SEC = 600  # 10 minutes


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
        except Exception:
            pass

    # 1) run project tests (S2: 带超时包装)
    if validation.action == "pass" and changed:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(val_mod.run_project_tests, cwd=cwd)
                try:
                    test_result = fut.result(timeout=_REVIEW_TIMEOUT_SEC)
                except concurrent.futures.TimeoutExpired:
                    quality["warnings"].append(f"审查超时(>{_REVIEW_TIMEOUT_SEC}s) — 不默认通过, 升GATE2兜底")
                    quality["failure_kind"] = "review_timeout"
                    quality["confidence"] = max(0.0, quality.get("confidence", 0.5) - 0.4)
                    validation.action = "retry"
                    validation.unverified.append("测试执行超时: 不默认通过, 需人工兜底")
                    _record_review_failure("test_timeout")
                    return
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
        except Exception as e:
            quality["warnings"].append(f"test execution error: {e}")

    # 2) multi-model review: 2+ models independently review changed files
    # ponytail: 小改动跳过审查 — 单文件 + <50行diff 不值得额外90s开销
    if validation.action == "pass" and changed and not _is_trivial_change(changed, cwd):
        try:
            writer_model = agent_cfg.get("model", "")
            agents_all = disp_mod.load_agents()
            # S3: 用 _all_agents_list 一次取全池, 去重复分支
            all_pool = disp_mod._all_agents_list(agents_all)
            reviewer_models = [
                a['model'] for a in all_pool
                if a['model'] != writer_model and disp_mod.agent_api_available(a)][:2]

            if reviewer_models:
                rev_files = []; rev_models = []; all_issues = []
                review_failed = False
                for f in changed[:3]:
                    # S2: 多模型审查带超时
                    try:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                            fut = ex.submit(val_mod.multi_model_review,
                                filepath=f, models=reviewer_models, cwd=cwd, diff_only=True)
                            review = fut.result(timeout=_REVIEW_TIMEOUT_SEC)
                    except concurrent.futures.TimeoutExpired:
                        quality["warnings"].append("多模型审查超时 — 不默认通过")
                        quality["failure_kind"] = "review_timeout"
                        quality["confidence"] = max(0.0, quality.get("confidence", 0.5) - 0.4)
                        validation.action = "retry"
                        validation.unverified.append("多模型审查超时: 不进入验收")
                        _record_review_failure("multi_review_timeout")
                        return
                    rev_files.append(f)
                    rev_models = review.get("models_used", [])
                    issues = review.get("issues", [])
                    if issues:
                        crit = [i for i in issues if i.get("severity") == "critical"]
                        warns = [i for i in issues if i.get("severity") == "warning"]
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
                    all_issues.extend(issues)
                    if review.get("verdicts"):
                        needs_fix = [v for v in review["verdicts"]
                                     if v.get("verdict") == "needs_fix"]
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
                    task_desc=task.description,
                    raw_output=exec_result.raw_output,
                    changed_files=changed, writer_level=level,
                    writer_model=writer_model, cwd=cwd)
                if review.get("issues"):
                    crit = [i for i in review["issues"]
                            if i.get("severity") == "critical"]
                    warns = [i for i in review["issues"]
                             if i.get("severity") == "warning"]
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


def check_review_fail_limit(project_id: str = "", current_retries: int = 0) -> dict:
    """D1: 检查审查失败是否触顶。

    Returns: {"blocked": bool, "action": "continue|escalate_to_gate2", "remaining": int}
    """
    remaining = _REVIEW_MAX_AUTO_FIX - current_retries
    if remaining <= 0:
        return {"blocked": True, "action": "escalate_to_gate2", "remaining": 0,
                "reason": f"审查自动修已达上限({_REVIEW_MAX_AUTO_FIX}轮), 升GATE2人工兜底"}
    return {"blocked": False, "action": "continue", "remaining": remaining}
