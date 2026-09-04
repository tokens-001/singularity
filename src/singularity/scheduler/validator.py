"""validator.py — validation pipeline. v2: run_project_tests + crossover_review."""
from __future__ import annotations
import json, re, subprocess
from dataclasses import dataclass, field
from typing import Optional
from singularity.scheduler import config
from singularity.scheduler.snapshot import Snapshot

_KNOWN_VERDICTS = {"人工复核", "注意", "信息不足", "阻断"}

_DANGEROUS_PATTERNS = [
    re.compile(r"rm\s+-rf\s+/"), re.compile(r"curl.*\|.*sh"), re.compile(r"sudo\s+rm"),
    re.compile(r"chmod\s+777"), re.compile(r">\s*/dev/sda"), re.compile(r"mkfs\."),
    re.compile(r"dd\s+if="), re.compile(r"DROP\s+TABLE", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM", re.IGNORECASE),
]

_HUMAN_REVIEW_PATTERNS = [
    re.compile(r"^-[^+]*security", re.MULTILINE),
    re.compile(r"^-[^+]*auth", re.MULTILINE),
    re.compile(r"^-[^+]*permission", re.MULTILINE),
]

@dataclass
class ValidationReport:
    verdict: str = "未知"; action: str = "pass"
    validate_verdict: str = ""; validate_reason: str = ""
    gate_passed: Optional[bool] = None; gate_message: str = ""
    human_review_required: bool = False
    unverified: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    turns_used: int = 0; confidence: float = 0.0
    quality_signals: dict = field(default_factory=dict)
    # D3: GATE3 分级路由 (QA 建议, Observer 裁定)
    fix_route: str = ""  # impl|design|note

def validate(candidate, gate_required, task_type, changed_files, snap, turn, max_turns):
    report = ValidationReport(turns_used=turn)
    for pat in _DANGEROUS_PATTERNS:
        if pat.search(candidate):
            report.verdict = "阻断"; report.action = "abort"
            report.unverified.append(f"L1: {pat.pattern}"); return report
    # 修复 #2: 执行器未产出任何文件 → 硬判失败逼重试, 不默认通过
    if not changed_files:
        report.verdict = "信息不足"
        report.action = "retry" if turn < max_turns else "abort"
        report.unverified.append("执行器未产出任何文件 (changed_files 空)")
        return report
    if gate_required or _gate_check_by_files(changed_files):
        g = _run_gate(); report.gate_passed = g.get("passed"); report.gate_message = g.get("message","")
        if not g.get("passed"):
            report.verdict = "gate失败"; report.action = "rollback" if turn >= max_turns else "retry"
            report.unverified.append(f"gate failed: {report.gate_message}"); return report
    for pat in _HUMAN_REVIEW_PATTERNS:
        if pat.search(candidate):
            report.human_review_required = True
            report.unverified.append(f"L3: {pat.pattern}"); break
    v = _run_validate(candidate)
    report.validate_verdict = v.get("verdict","未知"); report.validate_reason = v.get("verdict_reason",""); report.evidence = v
    _annotate_unverified(report, task_type, changed_files)
    # ── 硬规则检查 (非 LLM) ──
    hard = _hard_diff_rules(changed_files, cwd=str(config.PROJECT_ROOT))
    if hard.get("issues"):
        report.hard_rule_issues = hard["issues"]
        for iss in hard["issues"]:
            report.unverified.append(f"HardRule[{iss['rule']}]: {iss['summary']}")
        if not hard.get("passed"):
            report.verdict = "阻断"; report.action = "abort"
            return report

    if report.human_review_required: report.verdict = "阻断"; report.action = "abort"
    elif report.validate_verdict == "人工复核": report.verdict = "人工复核"; report.action = "retry" if turn < max_turns else "abort"
    elif report.validate_verdict == "信息不足": report.verdict = "信息不足"; report.action = "retry" if turn < max_turns else "abort"
    elif report.validate_verdict == "未知":
        # S5: 校验脚本超时/解析失败/不存在 → 不默认通过, 重试或阻断 (D1: 安全项绝不放行)
        report.verdict = "未知"; report.action = "retry" if turn < max_turns else "abort"
        report.unverified.append(f"validate 未知结果: {report.validate_reason}")
    elif report.validate_verdict == "注意": report.verdict = "通过"; report.action = "pass"
    else: report.verdict = "通过"; report.action = "pass"
    return report

def _run_validate(candidate):
    if not config.VALIDATE_SCRIPT.exists(): return {"verdict":"未知","verdict_reason":"validate.py not found"}
    try:
        p = subprocess.run(["python3",str(config.VALIDATE_SCRIPT),candidate,"--json"], capture_output=True,text=True,timeout=config.VALIDATE_TIMEOUT)
        return json.loads(p.stdout) if p.returncode==0 else {"verdict":"未知","verdict_reason":f"exit={p.returncode}"}
    except subprocess.TimeoutExpired: return {"verdict":"未知","verdict_reason":"timeout"}
    except (json.JSONDecodeError,Exception): return {"verdict":"未知","verdict_reason":"parse error"}

def _run_gate():
    if not config.EVAL_SCRIPT.exists(): return {"passed":True,"message":"no eval.py"}
    try:
        p = subprocess.run(["python3",str(config.EVAL_SCRIPT),"--gate","--json"], capture_output=True,text=True,timeout=config.GATE_TIMEOUT)
        d = json.loads(p.stdout) if p.stdout else {}; g = d.get("gate",{})
        return {"passed":g.get("passed",True),"message":g.get("message",f"exit={p.returncode}")}
    except subprocess.TimeoutExpired: return {"passed":False,"message":f"gate timeout"}
    except Exception as e: return {"passed":False,"message":f"gate error:{e}"}

def _gate_check_by_files(changed_files):
    if not changed_files: return False
    for f in changed_files:
        if f.rsplit("/",1)[-1] in config.GATE_TRIGGER_FILES: return True
    return False

def _annotate_unverified(report, task_type, changed_files):
    if task_type == "bugfix": report.unverified.append("bugfix: no regression test")
    if task_type == "refactor": report.unverified.append("refactor: impact analysis skipped")
    if task_type == "feature": report.unverified.append("feature: diff_review v2 enabled")
    if not changed_files: report.unverified.append("no changed files")

def pre_execution_hook(task, snap): return []

def post_execution_hook(exec_result, snap):
    warnings, signals = [], {}
    conf = 0.5
    if exec_result is None: return {"warnings":["no result"],"quality_signals":{},"confidence":0.0,"failure_kind":"no_result"}
    raw = exec_result.raw_output or ""; changed = exec_result.changed_files or []
    out_len = len(raw); signals["output_length"] = out_len
    if out_len < 80: warnings.append("output <80 chars"); conf -= 0.2
    elif out_len > 500: conf += 0.1
    fc = len(changed); signals["changed_files_count"] = fc
    if fc > 10: warnings.append(f"too many files({fc})"); conf -= 0.15
    errs = sum(raw.count(m) for m in ["Traceback","Error:","error:","FAILED","Exception","exit=1"])
    signals["error_marker_count"] = errs
    fk = "error_output" if errs>3 else ("ok" if errs==0 else "uncertain")
    if errs>3: warnings.append(f"{errs} error markers"); conf -= 0.2
    elif errs>0: conf -= 0.05*errs
    if any(kw in raw for kw in ["passed","PASSED","exit=0"]): signals["has_verification"] = True; conf += 0.15
    if conf<0.3: fk = "low_quality"
    elif conf<0.5 and fk=="ok": fk = "uncertain"
    return {"warnings":warnings,"quality_signals":signals,"confidence":max(0.0,min(1.0,conf)),"failure_kind":fk}


# ═══════════════════════════════════════════════════════════════
# v2: Independent tests + crossover review
# ═══════════════════════════════════════════════════════════════

def run_project_tests(cwd=None):
    """Run project test suite (pytest->unittest->npm). Returns {passed,total,failures,output,runner}."""
    import sys, re as _re
    root = cwd or str(config.PROJECT_ROOT)
    result = {"passed":True,"total":0,"failures":0,"output":"","runner":""}
    _py = sys.executable  # ponytail: 用当前Python，不用硬编码python3（uv run下python3可能没pytest）
    runners = [
        ([_py,"-m","pytest","-q","--tb=short"],"pytest"),
        ([_py,"-m","unittest","discover","-q"],"unittest"),
        (["npm","test","--","--silent"],"npm"),
    ]
    for cmd, name in runners:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=root)
            output = (r.stdout + "\n" + r.stderr)[:4000]
            if "no tests ran" in output.lower(): continue
            if r.returncode != 0 and name != "npm":
                result["passed"] = False; result["failures"] = r.returncode
                result["output"] = output; result["runner"] = name; return result
            if name == "pytest" and r.returncode == 0:
                mp = _re.search(r'(\d+)\s+passed', output)
                mf = _re.search(r'(\d+)\s+failed', output)
                if mp:
                    result["total"] = int(mp.group(1)) + (int(mf.group(1)) if mf else 0)
                    result["failures"] = int(mf.group(1)) if mf else 0
                    result["passed"] = result["failures"] == 0
                result["output"] = output; result["runner"] = name; return result
            if r.returncode == 0:
                result["output"] = output; result["runner"] = name; return result
        except FileNotFoundError: continue
        except Exception: continue
    result["output"] = "no test runner found (pytest/unittest/npm)"
    result["runner"] = "none"
    return result


def _hard_diff_rules(changed_files: list[str], diff_text: str = "", cwd=None) -> dict:
    """硬规则检查（非 LLM）：检测删除的测试、弱化的安全、裸 except。

    Returns: {"issues": [...], "passed": bool}
    """
    import subprocess as _sp
    from pathlib import Path as _Path
    issues = []
    root = _Path(cwd) if cwd else config.PROJECT_ROOT

    # 1. 检测被删除的文件 (仅检查 changed_files 中标记为删除的文件)
    for f in changed_files[:]:
        fp = root / f
        if not fp.exists():
            if f.startswith("test_") or "/test_" in f:
                issues.append({"severity": "critical",
                               "summary": f"测试文件缺失或已删除: {f}",
                               "rule": "no-delete-tests"})
            if any(kw in f for kw in ("security", "auth", "permission", "csrf")):
                issues.append({"severity": "critical",
                               "summary": f"安全相关文件缺失或已删除: {f}",
                               "rule": "no-delete-security-files"})

    # 2. 检测裸 except
    for f in changed_files:
        fp = root / f
        if not fp.exists() or not f.endswith(".py"):
            continue
        try:
            content = fp.read_text()
            lines = content.splitlines()
            for i, line in enumerate(lines, 1):
                if line.strip() == "except:":
                    issues.append({"severity": "warning",
                                   "summary": f"{f}:{i} 裸 except，应指定异常类型",
                                   "rule": "no-bare-except"})
        except Exception:
            continue

    # 3. 检测安全边界弱化 (文件 diff 中移除的 auth/security 相关行)
    if (root / ".git").exists():
        for f in changed_files:
            if not f.endswith(".py"):
                continue
            fp = root / f
            if not fp.exists():
                continue
            try:
                r = _sp.run(["git", "diff", "HEAD", "--", f], capture_output=True, text=True,
                            timeout=15, cwd=str(root))
                if r.returncode == 0:
                    diff = r.stdout
                    removed_auth = [l for l in diff.splitlines()
                                   if l.startswith("-") and any(kw in l for kw in
                                   ("require_auth", "csrf_token", "require_write", "permission"))]
                    if removed_auth:
                        issues.append({"severity": "warning",
                                       "summary": f"{f}: 移除了 {len(removed_auth)} 处认证/权限检查",
                                       "rule": "no-weaken-security"})
            except Exception:
                continue

    return {"issues": issues, "passed": len([i for i in issues if i["severity"] == "critical"]) == 0}


def crossover_review(task_desc, raw_output, changed_files, writer_level, writer_model="", cwd=None):
    """Use a DIFFERENT model to review agent output. Returns {issues,verdict,summary}."""
    if not changed_files:
        return {"issues":[],"verdict":"pass","summary":"no file changes"}

    review_level = writer_level

    # Get git diff
    diff_text = ""
    try:
        r = subprocess.run(["git","diff","--stat"]+changed_files,
                         capture_output=True,text=True,timeout=10,cwd=cwd or str(config.PROJECT_ROOT))
        diff_text = (r.stdout or "")[:3000]
        if diff_text:
            r2 = subprocess.run(["git","diff"]+changed_files,
                              capture_output=True,text=True,timeout=10,cwd=cwd or str(config.PROJECT_ROOT))
            diff_text += "\n" + (r2.stdout or "")[:5000]
    except Exception as _e:
        logging.getLogger(__name__).warning("git diff failed: %s", _e)

    if not diff_text.strip():
        return {"issues":[],"verdict":"pass","summary":"empty diff"}

    files_list = ", ".join(changed_files[:10])
    prompt = f"""Code review:

Task: {task_desc[:500]}
Files: {files_list}
Diff:
{diff_text[:6000]}

Check: logic errors, security, unnecessary changes, missed call sites.
Output ONLY JSON: {{"issues":[{{"severity":"critical|warning|info","line":approx,"detail":"..."}}],"verdict":"pass|retry|abort","summary":"one line"}}
No issues? {{"issues":[],"verdict":"pass","summary":"no issues"}}
JSON:"""

    try:
        from . import dispatcher as _disp
        agents = _disp.load_agents()
        chain = _disp.pick_agent_fallback_chain(
            agents, review_level,
            exclude={writer_model} if writer_model else None)
        if not chain:
            return {"issues":[],"verdict":"pass","summary":f"no reviewer at {review_level}"}
        result = _disp.dispatch(prompt, review_level, f"review_{writer_model or '?'}",
                               {review_level:[chain[0]]}, cwd=cwd or "")
        raw = result.executor_result.raw_output if result and result.executor_result else ""
    except Exception as e:
        return {"issues":[],"verdict":"pass","summary":f"review call failed: {e}"}

    try:
        m = re.search(r'\{[^{}]*"issues"[^{}]*\}', raw, re.DOTALL)
        if m:
            d = json.loads(m.group())
            return {"issues":d.get("issues",[]),"verdict":d.get("verdict","pass"),
                    "summary":d.get("summary",raw[:200])}
    except Exception as _e:
        logging.getLogger(__name__).warning("JSON parse validation failed: %s", _e)
    return {"issues":[],"verdict":"pass","summary":raw[:200] if raw else "no result"}


def multi_model_review(filepath: str, models: list[str] = None, cwd: str = None,
                       max_chunk_lines: int = 300, diff_only: bool = False) -> dict:
    """多模型并行独立审查一个文件。分段→并行派发→汇总。

    Args:
        filepath: 要审查的文件路径(相对项目根)
        models: 模型名列表, 默认用强力层前3个可用模型
        cwd: 工作目录
        max_chunk_lines: 每段最大行数
        diff_only: True=只审查 git diff (轻量,互补 crossover), False=审查全文

    Returns:
        {issues:[{model,severity,line,detail}], verdicts:[{model,verdict}],
         summaries:[{model,summary}], models_used:[str], elapsed:float}
    """
    import concurrent.futures, time as _time
    from pathlib import Path as _Path

    root = cwd or str(config.PROJECT_ROOT)

    # S4: review_level 在闭包 review_chunk 中被引用, 必须在此定义 (两档后统一 "any")
    review_level = "any"

    if diff_only:
        # 获取该文件的 git diff
        try:
            r = subprocess.run(["git", "diff", filepath],
                             capture_output=True, text=True, timeout=10, cwd=root)
            code = (r.stdout or "").strip()
            if not code:
                return {"issues":[],"verdicts":[],"summaries":[],
                        "models_used":[],"elapsed":0,"file":filepath,"mode":"diff","lines":0}
        except Exception:
            code = ""
        mode = "diff"
    else:
        fpath = _Path(root) / filepath
        if not fpath.exists():
            return {"issues":[],"verdicts":[],"summaries":[],
                    "models_used":[],"elapsed":0,"error":f"file not found: {filepath}"}
        code = fpath.read_text()
        mode = "full"

    lines = code.split('\n'); total_lines = len(lines)
    from . import dispatcher as _disp
    agents = _disp.load_agents()

    # 选模型: 指定 > 强力层可用 > 前3个
    if models:
        model_cfgs = []
        for name in models:
            for a in (agents.get("any",[]) or sum((v for v in agents.values() if isinstance(v,list)),[])):
                if a.get("model") == name and _disp.agent_api_available(a):
                    model_cfgs.append(a); break
    else:
        model_cfgs = [a for a in agents.get("any",[]) if _disp.agent_api_available(a)][:3]

    if not model_cfgs:
        return {"issues":[],"verdicts":[],"summaries":[],"models_used":[],"elapsed":0,"error":"no models available"}

    # 分段
    eff_chunk = max_chunk_lines if mode == "full" else max(max_chunk_lines, total_lines)
    chunks = []
    for i in range(0, total_lines, eff_chunk):
        end = min(i + eff_chunk, total_lines)
        chunks.append((f"L{i+1}-L{end}", '\n'.join(lines[i:end])))

    # 并行审查
    reviews = []
    start_time = _time.time()
    
    def review_chunk(chunk_data):
        chunk_label, chunk_content = chunk_data
        try:
            chunk_prompt = f"""File review for {filepath} ({chunk_label}):

Code:
```
{chunk_content[:3000]}
```

Check: logic errors, security, style, performance, correctness.
Output ONLY JSON: {{"issues":[{{"severity":"critical|warning|info","line":approx,"detail":"..."}}],"verdict":"pass|retry|abort","summary":"one line"}}
No issues? {{"issues":[],"verdict":"pass","summary":"no issues"}}
JSON:"""
            
            result = _disp.dispatch(chunk_prompt, review_level, f"mmr_{name[:8]}",
                                   {review_level:[cfg]}, cwd=root)
            raw = result.executor_result.raw_output if result and result.executor_result else ""
            
            try:
                m = re.search(r'\{[^{}]*"issues"[^{}]*\}', raw, re.DOTALL)
                if m:
                    d = json.loads(m.group())
                    return {"model": cfg.get("model", "unknown"),
                           "issues": d.get("issues", []),
                           "verdict": d.get("verdict", "pass"),
                           "summary": d.get("summary", raw[:200])}
            except Exception as _e:
                logging.getLogger(__name__).warning("model review failed: %s", _e)
            return {"model": cfg.get("model", "unknown"),
                   "issues": [], "verdict": "pass", "summary": raw[:200]}
        except Exception as e:
            return {"model": cfg.get("model", "unknown"),
                   "issues": [], "verdict": "pass", "summary": f"chunk review failed: {e}"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(model_cfgs)) as executor:
        future_to_model = {
            executor.submit(review_chunk, chunk_data): cfg.get("model", "unknown")
            for chunk_data in chunks
            for cfg in model_cfgs
        }
        
        for future in concurrent.futures.as_completed(future_to_model):
            reviews.append(future.result())

    elapsed = _time.time() - start_time
    
    # 汇总结果
    all_issues = []
    all_verdicts = []
    all_summaries = []
    models_used = list(set(r["model"] for r in reviews))
    
    for r in reviews:
        all_issues.extend([{"model": r["model"], **issue} for issue in r["issues"]])
        all_verdicts.append({"model": r["model"], "verdict": r["verdict"]})
        all_summaries.append({"model": r["model"], "summary": r["summary"]})
    
    return {
        "issues": all_issues,
        "verdicts": all_verdicts,
        "summaries": all_summaries,
        "models_used": models_used,
        "elapsed": elapsed,
        "chunks_processed": len(chunks),
        "mode": mode,
        "total_lines": total_lines
    }


def incremental_review(file_path: str, old_content: str, new_content: str, 
                      models: list[str] = None, cwd: str = None) -> dict:
    """增量审查：对比新旧内容差异，重点审查变更部分。
    
    Args:
        file_path: 文件路径
        old_content: 旧内容
        new_content: 新内容  
        models: 模型列表
        cwd: 工作目录
        
    Returns:
        审查结果字典
    """
    import difflib
    
    # 计算差异
    diff = list(difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=3  # 上下文行数
    ))
    
    if not diff:
        return {"issues": [], "verdict": "pass", "summary": "no changes detected"}
    
    diff_text = ''.join(diff)
    
    # 使用crossover_review逻辑进行审查
    return crossover_review(
        f"Incremental review of {file_path}",
        f"Diff:\n{diff_text}",
        [file_path],
        "any",  # 两档后统一 any
        "",
        cwd
    )


def security_review(code: str, file_path: str = "", severity_filter: str = "all") -> dict:
    """专门的安全审查函数。
    
    Args:
        code: 代码内容
        file_path: 文件路径(用于上下文)
        severity_filter: "all", "high", "critical"
        
    Returns:
        安全审查结果
    """
    security_patterns = [
        {"pattern": r"(eval|exec)\s*\(", "severity": "critical", "desc": "危险代码执行函数"},
        {"pattern": r"subprocess\.(call|run|Popen)", "severity": "warning", "desc": "子进程执行"},
        {"pattern": r"(os\.system|os\.popen)", "severity": "critical", "desc": "系统命令执行"},
        {"pattern": r"open\([^)]*\"w|write", "severity": "warning", "desc": "文件写入操作"},
        {"pattern": r"(password|secret|token|key)\s*=", "severity": "warning", "desc": "硬编码敏感信息"},
        {"pattern": r"sql\s*[+=].*|execute\s*\(", "severity": "warning", "desc": "SQL查询执行"},
        {"pattern": r"(allow_|enable_|skip_|disable_)(auth|verify|check)", "severity": "critical", "desc": "安全检查绕过"},
    ]
    
    issues = []
    lines = code.split('\n')
    
    for i, line in enumerate(lines, 1):
        for pattern_info in security_patterns:
            if re.search(pattern_info["pattern"], line, re.IGNORECASE):
                severity = pattern_info["severity"]
                if severity_filter != "all":
                    if severity_filter == "critical" and severity != "critical":
                        continue
                    if severity_filter == "high" and severity not in ["critical", "high"]:
                        continue
                
                issues.append({
                    "severity": severity,
                    "line": i,
                    "detail": f"{pattern_info['desc']}: {line.strip()}",
                    "pattern": pattern_info["pattern"]
                })
    
    # 如果有高危模式，需要人工复核
    critical_issues = [i for i in issues if i["severity"] == "critical"]
    verdict = "abort" if critical_issues else "pass"
    
    return {
        "issues": issues,
        "verdict": verdict,
        "summary": f"Found {len(issues)} security issues ({len(critical_issues)} critical)" if issues else "No security issues detected"
    }


# ═══════════════════════════════════════════════════════════════
# D3: GATE3 分级路由 (按 issue.fix_route 决定打回去哪)
# ═══════════════════════════════════════════════════════════════

def grade_fix_route(issues: list[dict], overall_verdict: str) -> str:
    """D3: 按 issues 严重度计算 fix_route.

    - 有架构级缺陷 (severity=critical + fix_route=design) → "design"
    - 多数为实现级 bug (severity=warning/bug) → "impl"
    - 仅有 suggestion → "note"
    - 综合 overall_verdict: no_go → 默认 "impl" (不轻易升 GATE2)
    """
    has_design = any(
        i.get("fix_route") == "design" or i.get("severity") == "critical"
        for i in issues)
    has_bugs = any(
        i.get("fix_route") == "impl" or i.get("severity") in ("bug", "warning")
        for i in issues)
    only_notes = all(
        i.get("fix_route") == "note" or i.get("severity") == "info"
        for i in issues)

    if has_design:
        return "design"
    if only_notes and overall_verdict != "no_go":
        return "note"
    if has_bugs or overall_verdict == "no_go":
        return "impl"
    return "note"


def build_qa_report(passed: list, issues: list, verdict: str, verdict_reason: str) -> dict:
    """D3: 构建 QA 报告 (符合修订案 schema)。"""
    return {
        "passed": passed,
        "issues": [{
            "id": i.get("id", f"Q{idx:03d}"),
            "severity": i.get("severity", "warning"),
            "fix_route": i.get("fix_route", grade_fix_route([i], verdict)),
            "file": i.get("file", ""),
            "description": i.get("detail", i.get("description", "")),
            "suggested_fix": i.get("suggested_fix", ""),
        } for idx, i in enumerate(issues)],
        "summary": {
            "total_checks": len(passed) + len(issues),
            "passed": len(passed),
            "failed": len(issues),
            "verdict": verdict,
            "verdict_reason": verdict_reason,
        },
    }