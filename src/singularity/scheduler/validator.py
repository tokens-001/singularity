"""validator.py — validation pipeline. v2: run_project_tests + crossover_review."""
from __future__ import annotations
import json, logging, re, subprocess
from dataclasses import dataclass, field
from typing import Optional
from singularity.scheduler import config
from singularity.scheduler.snapshot import Snapshot

_KNOWN_VERDICTS = {"人工复核", "注意", "信息不足", "阻断"}

_DANGEROUS_PATTERNS = [
    re.compile(r"rm\s+-rf\s+/"), re.compile(r"curl.*\|.*sh"), re.compile(r"sudo\s+rm"),
    re.compile(r"chmod\s+777"), re.compile(r">\s*/dev/sda"), re.compile(r"mkfs\."),
    re.compile(r"dd\s+if="),
    # SQL 注入才拦: '; 逃逸出字符串后跟 DROP/DELETE。合法 CRUD 的 DELETE FROM ... WHERE ?=、幂等 DROP TABLE IF EXISTS 不拦
    re.compile(r"';\s*(?:DROP\s+TABLE|DELETE\s+FROM)", re.IGNORECASE),
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

def validate(candidate, gate_required, task_type, changed_files, snap, turn, max_turns, cwd=None):
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
    # cwd 必须是任务执行的 worktree 路径, 不是主仓库根——否则新建的 test_*.py 会被误判"已删除"
    _base = _diff_base(snap)
    if not _base:
        report.unverified.append("审查基准不可用(快照非 git 型) → diff 类硬规则检查未执行")
    hard = _hard_diff_rules(changed_files, cwd=cwd or str(config.PROJECT_ROOT), base=_base)
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
    elif report.validate_verdict in ("注意", "通过"):
        report.verdict = "通过"; report.action = "pass"
    else:
        # 兜底不再无条件放行: 意外 verdict 值 → 保守判未知, 不默认通过
        report.verdict = "未知"; report.action = "retry" if turn < max_turns else "abort"
        report.unverified.append(f"validate 意外结果: {report.validate_reason}")
    return report

def _run_validate(candidate):
    if not config.VALIDATE_SCRIPT.exists(): return {"verdict":"未知","verdict_reason":"validate.py not found"}
    try:
        p = subprocess.run(["python3",str(config.VALIDATE_SCRIPT),candidate,"--json"], capture_output=True,text=True,timeout=config.VALIDATE_TIMEOUT)
        return json.loads(p.stdout) if p.returncode==0 else {"verdict":"未知","verdict_reason":f"exit={p.returncode}"}
    except subprocess.TimeoutExpired: return {"verdict":"未知","verdict_reason":"timeout"}
    except (json.JSONDecodeError,Exception): return {"verdict":"未知","verdict_reason":"parse error"}

def _run_gate():
    # 保守化: eval.py 不存在 = gate 未执行, 不是 gate 通过; 缺 passed 字段也默认不通过
    if not config.EVAL_SCRIPT.exists(): return {"passed":False,"message":"eval.py 不存在 (gate 未执行)"}
    try:
        p = subprocess.run(["python3",str(config.EVAL_SCRIPT),"--gate","--json"], capture_output=True,text=True,timeout=config.GATE_TIMEOUT)
        d = json.loads(p.stdout) if p.stdout else {}; g = d.get("gate",{})
        return {"passed":g.get("passed",False),"message":g.get("message",f"exit={p.returncode}")}
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
    import sys, os as _os, re as _re
    root = cwd or str(config.PROJECT_ROOT)
    result = {"passed":True,"total":0,"failures":0,"output":"","runner":""}
    # 目录不存在也要**说清楚**。不查的话三个 runner 全在 subprocess 里抛异常被
    # `except Exception: continue` 吞掉，最后报"三个都启动不了"—— 排查方向全错。
    if not _os.path.isdir(root):
        result["output"] = f"测试目录不存在: {root}"
        result["runner"] = "none"
        return result
    _py = sys.executable  # ponytail: 用当前Python，不用硬编码python3（uv run下python3可能没pytest）
    runners = [
        ([_py,"-m","pytest","-q","--tb=short"],"pytest"),
        ([_py,"-m","unittest","discover","-q"],"unittest"),
        (["npm","test","--","--silent"],"npm"),
    ]
    ran_but_empty: list[str] = []   # 跑起来了、但**没找到测试**的 runner
    for cmd, name in runners:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=root)
            output = (r.stdout + "\n" + r.stderr)[:4000]
            if "no tests ran" in output.lower():
                ran_but_empty.append(name)
                continue
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
    # **"跑不起来"和"没找到测试"要分开说** —— 两者的排查方向完全不同：
    # 前者查环境，后者查"测试文件在不在"。2026-09-12 探路2 的 T4 实测：
    # 报的是"无可用 runner (pytest/unittest/npm 均不可用)"，而真相是
    # **它的 worktree 里压根没有测试文件**（T2 写好的测试因为超时没合并进来）。
    # 同一族见防御模式 §52：拦了/没跑，但说不清是什么。
    result["output"] = (
        f"runner 跑得起来（{'/'.join(ran_but_empty)}），但**没找到测试文件**"
        if ran_but_empty else
        "no test runner found (pytest/unittest/npm 都启动不了)")
    result["runner"] = "none"
    return result


def _diff_base(snap) -> str:
    """审查取 diff 的基准 ref（2026-09-11 审计 P0-1）。

    worktree 路径下，改动在 validate **之前**就被 `_process_planner_or_merge` 里的
    `commit_wt` 提交了，所以裸 `git diff` / `git diff HEAD` **恒为空** —— 审查看不到
    任何改动。改用**执行前快照**的 ref 当基准。

    `method="copy"` 的快照 ref 是目录路径不是 git ref → 返回 ""，表示取不到基准；
    调用方必须如实记进 unverified，不能当成"检查通过"。
    """
    if snap is None:
        return ""
    ref = getattr(snap, "ref", "") or ""
    return ref if ref and getattr(snap, "method", "") == "git" else ""


def _hard_diff_rules(changed_files: list[str], diff_text: str = "", cwd=None, base: str = "") -> dict:
    """硬规则检查（非 LLM）：检测删除的测试、弱化的安全、裸 except。

    base: diff 基准 ref（见 _diff_base）。空 = 取不到基准，diff 类检查**不做**，
    由调用方披露 —— 不能静默当作通过。

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
    #    基准必须是**执行前快照**：worktree 里改动已被 commit_wt 提交，`git diff HEAD` 恒空。
    if base and (root / ".git").exists():
        for f in changed_files:
            if not f.endswith(".py"):
                continue
            fp = root / f
            if not fp.exists():
                continue
            try:
                r = _sp.run(["git", "diff", base, "--", f], capture_output=True, text=True,
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


def _extract_json_obj(text: str):
    """从模型输出提取首个 JSON 对象(支持嵌套 issues 数组)。失败返回 None。"""
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _norm_verdict(v, default: str) -> str:
    """LLM 返回的外层 verdict 归一化后再比：小写 + 去空白，缺失/空 → default。

    判官是模型，输出会在大小写/空格上飘（"Critical" / "CRITICAL" / " critical "）。
    精确匹配会让它匹配不上 → 消费端把 findings 整条丢弃 → **真漏洞随代码合并**。

    本仓库修过一轮同类 fail-open（09-10 修的是 severity 字段，见修复归档 #2），
    这次漏的是 verdict 字段 —— 同一个病，换了字段。
    注意：default 仍是放行档（clean/accepted）。模型不吐 verdict 时仍会静默通过，
    这是**已知的 fail-open 残留**，要改得先有测量，本次不动。
    """
    if v is None:
        return default
    s = str(v).strip().lower()
    return s or default


def crossover_review(task_desc, raw_output, changed_files, writer_level, writer_model="", cwd=None,
                     base_ref=""):
    """Use a DIFFERENT model to review agent output. Returns {issues,verdict,summary}.

    ⚠️ **`base_ref` 必须传**（worktree 路径上）。改动在 `validate()` 之前就被
    `commit_wt` 提交了 → 裸 `git diff`（跟 HEAD 比）对已提交的改动**恒为空** →
    下面那句空 diff 的早退会返回 `verdict: "pass"` —— **审查静默漏过整份改动**。
    这是**同一个形状的第三处**：validator 的 multi_model_review 09-11 修过、
    orchestrator 的抢救 09-12 修过，这处没跟上。
    """
    if not changed_files:
        return {"issues":[],"verdict":"pass","summary":"no file changes"}

    review_level = writer_level

    # Get git diff —— 带基准
    _base = [base_ref] if base_ref else []
    diff_text = ""
    try:
        r = subprocess.run(["git","diff",*_base,"--stat",*changed_files],
                         capture_output=True,text=True,timeout=10,cwd=cwd or str(config.PROJECT_ROOT))
        diff_text = (r.stdout or "")[:3000]
        if diff_text:
            r2 = subprocess.run(["git","diff",*_base,*changed_files],
                              capture_output=True,text=True,timeout=10,cwd=cwd or str(config.PROJECT_ROOT))
            diff_text += "\n" + (r2.stdout or "")[:5000]
    except Exception as _e:
        logging.getLogger(__name__).warning("git diff failed: %s", _e)

    if not diff_text.strip():
        # 有基准 = 真的没改动，判 pass 没问题。
        # **没基准就别装**：可能只是看不见（已提交的改动裸 diff 看不到）。
        return {"issues":[],"verdict":"pass",
                "summary": "empty diff" if base_ref
                           else "empty diff（**没拿到基准**，这条结论不可信）"}

    files_list = ", ".join(changed_files[:10])
    # 角色定位/审查重点在 roles.toml [reviewer]（页面上可改）；这里只填动态内容和输出契约
    from .roles import get_role, get_phase_role
    review_role = get_role(get_phase_role("reviewing") or "reviewer")
    role_prompt = review_role.get_full_prompt() if review_role else "你是代码审查者。"
    prompt = f"""{role_prompt}

Task: {task_desc[:500]}
Files: {files_list}
Diff:
{diff_text[:6000]}

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
            return {"issues":[{"severity":"critical","line":0,
                     "detail":f"no reviewer at {review_level}"}],
                    "verdict":"retry","summary":f"no reviewer at {review_level}"}
        result = _disp.dispatch(prompt, review_level, f"review_{writer_model or '?'}",
                               {review_level:[chain[0]]}, cwd=cwd or "")
        raw = result.executor_result.raw_output if result and result.executor_result else ""
    except Exception as e:
        return {"issues":[{"severity":"critical","line":0,
                 "detail":f"review call failed: {e}"}],
                "verdict":"retry","summary":f"review call failed: {e}"}

    d = _extract_json_obj(raw)
    if d:
        return {"issues":d.get("issues",[]),"verdict":d.get("verdict","pass"),
                "summary":d.get("summary",raw[:200])}
    return {"issues":[{"severity":"critical","line":0,
             "detail":f"review output not JSON: {raw[:100]}"}],
            "verdict":"retry","summary":raw[:200] if raw else "no result"}


def multi_model_review(filepath: str, models: list[str] = None, cwd: str = None,
                       max_chunk_lines: int = 300, diff_only: bool = False,
                       requirements: str = "", base_ref: str = "") -> dict:
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
        # 获取该文件的 git diff。**必须带基准**（见 _diff_base）：worktree 里改动
        # 已被 commit_wt 提交，裸 `git diff` 恒为空 → 一个模型都不会被调，
        # 却返回 {"issues":[]} 让上层以为"审过且没问题"。
        try:
            r = subprocess.run(["git", "diff", base_ref, filepath] if base_ref
                             else ["git", "diff", filepath],
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
        enabled = (agents.get("any", []) or
                   sum((v for v in agents.values() if isinstance(v, list)), []))
        model_cfgs = []
        for name in models:
            found = None
            for a in enabled:
                if a.get("model") == name and _disp.agent_api_available(a):
                    found = a
                    break
            if found is None:
                # 不在启用池里 → 可能是 _review._expand_review_pool 补的注册表模型。
                # 不补这一步的话，传进来的名字会被**静默跳过**：扩了等于没扩，
                # 而且外面看到的是"multi-review 跑过了"。
                cand = {"model": name}          # agent_api_available 会就地补全 type 等
                if _disp.agent_api_available(cand) and cand.get("type"):
                    found = cand
            if found is not None:
                model_cfgs.append(found)
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
    
    def review_chunk(chunk_data, cfg):
        chunk_label, chunk_content = chunk_data
        model_name = cfg.get("model", "unknown")
        try:
            req_block = f"\nTask requirements:\n{requirements[:500]}\n" if requirements else ""
            chunk_prompt = f"""File review for {filepath} ({chunk_label}):{req_block}

Code:
```
{chunk_content[:3000]}
```

Check: logic errors, security, style, performance, correctness; AND requirement completeness — 逐条核对 Task requirements，需求明确要求但产出未实现或明显缩水的标 critical(含风格/响应式/适配等软性要求)，实现不完美但不影响需求的标 warning.
Output ONLY JSON: {{"issues":[{{"severity":"critical|warning|info","line":approx,"detail":"..."}}],"verdict":"pass|retry|abort","summary":"one line"}}
No issues? {{"issues":[],"verdict":"pass","summary":"no issues"}}
JSON:"""
            
            result = _disp.dispatch(chunk_prompt, review_level, f"mmr_{model_name[:8]}",
                                   {review_level:[cfg]}, cwd=root)
            raw = result.executor_result.raw_output if result and result.executor_result else ""
            
            d = _extract_json_obj(raw)
            if d:
                return {"model": model_name,
                       "issues": d.get("issues", []),
                       "verdict": d.get("verdict", "pass"),
                       "summary": d.get("summary", raw[:200])}
            return {"model": model_name,
                   "issues": [{"severity": "critical", "line": 0,
                               "detail": f"chunk review output not JSON: {raw[:100]}"}],
                   "verdict": "retry", "summary": raw[:200]}
        except Exception as e:
            return {"model": cfg.get("model", "unknown"),
                   "issues": [{"severity": "critical", "line": 0,
                               "detail": f"chunk review failed: {e}"}],
                   "verdict": "retry", "summary": f"chunk review failed: {e}"}

    # 不用 `with`: 退出时 shutdown(wait=True) 会 join，CLAUDE_CLI_TIMEOUT 就只是
    # "延迟判定"而非时限 —— 挂死的调用会把审查整段拖住。显式 shutdown(wait=False)。
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(model_cfgs))
    try:
        future_to_model = {
            _executor.submit(review_chunk, chunk_data, cfg): cfg.get("model", "unknown")
            for chunk_data in chunks
            for cfg in model_cfgs
        }

        done, not_done = concurrent.futures.wait(
            future_to_model, timeout=config.CLAUDE_CLI_TIMEOUT)
        for future in done:
            reviews.append(future.result())
        for future in not_done:
            reviews.append({"model": future_to_model[future], "issues": [],
                            "verdict": "abort", "summary": "chunk review timeout"})
    finally:
        _executor.shutdown(wait=False)

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


def qa_acceptance_review(constraints, diff_text, cwd, requirements=""):
    """QA 验收：用 qa_engineer 角色 system_prompt 对照约束清单验证代码改动。

    补 multi_model_review 的盲区：它查 bug/需求完整性，不查「约束是否满足」。

    Args:
        constraints: 约束清单 (list[str] 或 list[dict]，dict 取 rule/check)
        diff_text: 改动 diff 文本
        cwd: 工作目录

    Returns:
        {"verdict": "accepted|needs_fix", "verifications": [...], "summary": str}
    """
    from .roles import get_role
    from . import dispatcher as _disp

    qa = get_role('qa_engineer')
    if not qa:
        return {"verdict": "accepted", "verifications": [], "summary": "qa_engineer 角色未定义"}

    lines = []
    for c in constraints or []:
        if isinstance(c, str):
            lines.append(f"- {c}")
        elif isinstance(c, dict):
            rule = c.get('rule', c.get('text', ''))
            check = c.get('check', '')
            lines.append(f"- {rule}" + (f"（验证方式：{check}）" if check else ""))
    if not lines:
        return {"verdict": "accepted", "verifications": [], "summary": "无约束清单"}

    prompt = f"""{qa.get_full_prompt()}

【约束清单】逐条验证以下约束是否被满足：
{chr(10).join(lines)}

【代码改动 diff】
{diff_text[:6000] if diff_text else '(无 diff)'}

只输出 JSON：
{{"verification":[{{"constraint":"约束","status":"pass|fail|warning|uncertain","evidence":"证据","detail":"说明"}}],"summary":{{"verdict":"accepted|needs_fix","critical":0,"major":0,"minor":0,"recommendation":"一句话"}}}}

**一致性要求**：verdict 判 needs_fix 时，verification 里**必须至少有一条** status 为 fail
或 warning，并写清是哪条约束、差在哪。判了 needs_fix 却给不出具体条目，这个结论对下游
不可执行 —— 重试方只知道"要修"却不知道修什么，只会原样再来一遍。"""

    # 单模型验收（QA 验收不需多模型碰撞）
    agents = _disp.load_agents()
    model_cfgs = [a for a in _disp._all_agents_list(agents) if _disp.agent_api_available(a)][:1]
    if not model_cfgs:
        return {"verdict": "needs_fix", "verifications": [], "summary": "无可用 QA 模型 (未验收)"}

    try:
        result = _disp.dispatch(prompt, "any", "qa_acceptance", {"any": model_cfgs}, cwd=cwd)
        raw = result.executor_result.raw_output if result and result.executor_result else ""
    except Exception as e:
        return {"verdict": "needs_fix", "verifications": [], "summary": f"QA 验收调用失败: {e}"}

    d = _extract_json_obj(raw)
    if not d:
        return {"verdict": "needs_fix", "verifications": [], "summary": f"QA 输出非 JSON: {raw[:200]}"}

    verdict = _norm_verdict((d.get("summary") or {}).get("verdict"), "accepted")
    if verdict in ("rejected", "needs_fix"):
        verdict = "needs_fix"  # 归一化：qa_engineer 三档 verdict 里 rejected 同样触发修复
    return {"verdict": verdict, "verifications": d.get("verification", []), "summary": raw[:300]}


def security_audit_review(diff_text, cwd, requirements=""):
    """安全审计：用 security_auditor 角色 system_prompt 做 LLM 五维审计。

    补 security_review（正则）抓不到的复杂漏洞：权限越权/注入变体/隐私泄露。

    Args:
        diff_text: 改动 diff 文本
        cwd: 工作目录

    Returns:
        {"verdict": "clean|needs_fix", "findings": [...], "summary": str}
    """
    from .roles import get_role
    from . import dispatcher as _disp

    sa = get_role('security_auditor')
    if not sa:
        return {"verdict": "clean", "findings": [], "summary": "security_auditor 角色未定义"}

    prompt = f"""{sa.get_full_prompt()}

【代码改动 diff】
{diff_text[:6000] if diff_text else '(无 diff)'}

severity 判定标准 —— **只有 critical/high 会拦下合并**，别把设计不完整往上报：
- critical = 可被直接利用的漏洞（注入、越权、明文密钥、未校验的外部输入直达危险操作）
- high     = 明确的安全缺陷，需要真实攻击条件但仍应阻断（缺失鉴权、敏感信息进日志）
- medium   = 加固建议，当前不可直接利用（缺限流、错误信息过详细）
- low      = 最佳实践（依赖版本偏旧但无已知 CVE）

⚠️ "需求没要求的安全能力没做"（没做审计日志、没做字段加密…）是**需求范围**问题，
   不是本次改动的安全漏洞 —— 归 medium/low 并写进 remediation，不要标 critical。

只输出 JSON：
{{"findings":[{{"severity":"critical|high|medium|low","category":"auth|injection|secrets|dependency|privacy","cwe":"CWE-xxx","location":"文件:行号","description":"问题","remediation":"建议"}}],"summary":{{"verdict":"clean|needs_fix|critical","critical":0,"high":0,"medium":0,"low":0,"recommendation":"一句话"}}}}"""

    # 单模型审计（安全审计不需多模型碰撞）
    agents = _disp.load_agents()
    model_cfgs = [a for a in _disp._all_agents_list(agents) if _disp.agent_api_available(a)][:1]
    if not model_cfgs:
        return {"verdict": "needs_fix", "findings": [
            {"severity": "high", "category": "review_error", "cwe": "CWE-0",
             "location": "security_audit", "description": "无可用安全审计模型，未完成审计",
             "remediation": "配置安全审计模型；若持续失败需人工介入"}
        ], "summary": "无可用模型"}

    try:
        result = _disp.dispatch(prompt, "any", "security_audit", {"any": model_cfgs}, cwd=cwd)
        raw = result.executor_result.raw_output if result and result.executor_result else ""
    except Exception as e:
        # fail-closed: 安全审计失败不能标 clean（否则"没审就当安全"）。标 needs_fix + high 触发重试。
        return {"verdict": "needs_fix", "findings": [
            {"severity": "high", "category": "review_error", "cwe": "CWE-0",
             "location": "security_audit", "description": f"安全审计调用失败，未完成审计: {e}",
             "remediation": "重试安全审计；若持续失败需人工介入"}
        ], "summary": f"安全审计调用失败: {e}"}

    d = _extract_json_obj(raw)
    if not d:
        return {"verdict": "needs_fix", "findings": [
            {"severity": "high", "category": "review_error", "cwe": "CWE-0",
             "location": "security_audit", "description": "安全审计输出非 JSON，未完成审计",
             "remediation": "重试安全审计；若持续失败需人工介入"}
        ], "summary": f"安全审计输出非 JSON: {raw[:200]}"}

    verdict = _norm_verdict((d.get("summary") or {}).get("verdict"), "clean")
    if verdict in ("critical", "needs_fix"):
        verdict = "needs_fix"  # 归一化：critical 同样触发修复
    return {"verdict": verdict, "findings": d.get("findings", []), "summary": raw[:300]}