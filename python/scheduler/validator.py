"""validator.py — validation pipeline. v2: run_project_tests + crossover_review."""
from __future__ import annotations
import json, re, subprocess
from dataclasses import dataclass, field
from typing import Optional
from . import config
from .snapshot import Snapshot

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

def validate(candidate, gate_required, task_type, changed_files, snap, turn, max_turns):
    report = ValidationReport(turns_used=turn)
    for pat in _DANGEROUS_PATTERNS:
        if pat.search(candidate):
            report.verdict = "阻断"; report.action = "abort"
            report.unverified.append(f"L1: {pat.pattern}"); return report
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
    if report.human_review_required: report.verdict = "阻断"; report.action = "abort"
    elif report.validate_verdict == "人工复核": report.verdict = "人工复核"; report.action = "retry" if turn < max_turns else "abort"
    elif report.validate_verdict == "信息不足": report.verdict = "信息不足"; report.action = "retry" if turn < max_turns else "abort"
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
    import re as _re
    root = cwd or str(config.PROJECT_ROOT)
    result = {"passed":True,"total":0,"failures":0,"output":"","runner":""}
    runners = [
        (["python3","-m","pytest","-q","--tb=short"],"pytest"),
        (["python3","-m","unittest","discover","-q"],"unittest"),
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


def crossover_review(task_desc, raw_output, changed_files, writer_level, writer_model="", cwd=None):
    """Use a DIFFERENT model to review agent output. Returns {issues,verdict,summary}."""
    if not changed_files:
        return {"issues":[],"verdict":"pass","summary":"no file changes"}

    review_level = {"E":"E+","E+":"D"}.get(writer_level, "D")

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
    except Exception: pass

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
    except Exception: pass
    return {"issues":[],"verdict":"pass","summary":raw[:200] if raw else "no result"}
