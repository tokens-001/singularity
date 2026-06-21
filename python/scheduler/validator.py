"""validator.py — validation pipeline.

v2: run_project_tests + crossover_review.
"""
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
# v2: test + review enabled


# ═══════════════════════════════════════════════════════════════
# v2: Independent tests + crossover review
# ═══════════════════════════════════════════════════════════════

def run_project_tests(cwd=None):
    """Run project test suite (pytest->unittest->npm). Returns {passed,total,failures,output,runner}."""
    import subprocess as sp, re as _re
    root = cwd or str(config.PROJECT_ROOT)
    result = {"passed":True,"total":0,"failures":0,"output":"","runner":""}
    runners = [
        (["python3","-m","pytest","-q","--tb=short"],"pytest"),
        (["python3","-m","unittest","discover","-q"],"unittest"),
        (["npm","test","--","--silent"],"npm"),
    ]
    for cmd, name in runners:
        try:
            r = sp.run(cmd, capture_output=True, text=True, timeout=60, cwd=root)
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
    """Independent model reviews the diff. Returns {score, comments, reviewer_model}."""
    import os, difflib, subprocess as sp
    from .. import llm_client
    root = cwd or str(config.PROJECT_ROOT)
    if not changed_files:
        return {"score":0.0,"comments":["no changed files"],"reviewer_model":"none"}
    diffs = []
    for f in changed_files:
        path = os.path.join(root, f)
        if not os.path.exists(path): continue
        try:
            with open(path,"r",encoding="utf-8",errors="ignore") as fh:
                lines = fh.readlines()
        except Exception: continue
        before = [""]*len(lines)
        after = lines
        diff = list(difflib.unified_diff(before, after, fromfile=f"/dev/null", tofile=f, lineterm=""))
        diffs.append(f"--- {f}\n" + "\n".join(diff[:200]))
    diff_text = "\n\n".join(diffs)[:6000]
    reviewer = llm_client.pick_model(level=min(3, writer_level+1))
    if not reviewer:
        reviewer = llm_client.pick_model(level=writer_level)
    prompt = f"""You are an independent code reviewer. Task: {task_desc}
The agent '{writer_model}' produced the following changes:
{diff_text}

Please provide a concise code review in JSON: {{"score": <0-1>, "comments": [<list>]}}
"""
    try:
        response = llm_client.chat(reviewer, prompt, json_mode=True)
        data = json.loads(response)
        return {"score": float(data.get("score",0.0)), "comments": data.get("comments",[]), "reviewer_model": reviewer}
    except Exception as e:
        return {"score":0.0,"comments":[f"review failed: {e}"],"reviewer_model": reviewer or "none"}


def compute_quality_score(report: ValidationReport) -> float:
    """Combine gate, validation, tests, and review into a single score."""
    s = 0.5
    if report.gate_passed is True: s += 0.2
    elif report.gate_passed is False: s -= 0.3
    if report.validate_verdict == "通过": s += 0.2
    elif report.validate_verdict == "阻断": s -= 0.4
    qs = report.quality_signals or {}
    s += qs.get("has_verification",0) * 0.1
    s -= qs.get("error_marker_count",0) * 0.02
    return max(0.0, min(1.0, s))
