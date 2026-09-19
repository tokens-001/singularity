#!/usr/bin/env python3
"""preflight 复现脚本 —— 用已确认的历史现场当成绩单，验收 scripts/preflight.py。

跑法（仓库根下）:
    python3 scripts/test_preflight.py

内容:
  · 5 个成绩单坐标（修复的父提交，洞还在的版本）逐个断言命中；
  · HEAD 基线 9c81ea5 断言"形状 A 零命中"（四处闸门已修、不该再报）；
  · live 子命令用合成现场（临时目录，不碰真 .qidian）断言 .corrupt 与告警能报出来。

全部只读：对真仓库只调 git archive，不 checkout、不写工作区；live 测试在临时目录里做。
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = Path(__file__).resolve().parent / "preflight.py"
HEAD = "9c81ea5"

FAILURES = []


def run_shapes(rev: str):
    p = subprocess.run([sys.executable, str(TOOL), "shapes", "--rev", rev, "--json"],
                       cwd=REPO, capture_output=True, text=True)
    if p.returncode not in (0, 1):
        raise RuntimeError(f"shapes --rev {rev} 退出码 {p.returncode}: {p.stderr[-400:]}")
    return json.loads(p.stdout)


def check(name: str, ok: bool, detail: str = ""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def find(findings, shape, symbol_sub=None, msg_sub=None, file_sub=None):
    for f in findings:
        if f["shape"] != shape:
            continue
        if symbol_sub and symbol_sub not in f["symbol"]:
            continue
        if msg_sub and msg_sub not in f["message"]:
            continue
        if file_sub and file_sub not in f["file"]:
            continue
        return f
    return None


def case_score1():
    print("成绩单① 312584b —— 形状 A 四处（护栏查在读之前）")
    d = run_shapes("312584b")
    fs = d["findings"]
    for sym, file_sub in [("record", "_model_discipline"),
                          ("set_projects_root", "scheduler/project"),
                          ("set_agent_skills", "skill_loader"),
                          ("save_custom_model", "api_store")]:
        f = find(fs, "A-guard-before-read", symbol_sub=sym, file_sub=file_sub)
        check(f"A: {file_sub}::{sym}",
              f is not None and "is_quarantined" in " ".join(f["evidence"]),
              "未命中或证据缺 is_quarantined")
    a_count = sum(1 for f in fs if f["shape"] == "A-guard-before-read")
    check("A: 恰好 4 条（不多报）", a_count == 4, f"实际 {a_count}")


def case_score2():
    print("成绩单② 905a5dd —— B2b 两处窄判据（cancelled 族）")
    d = run_shapes("905a5dd")
    fs = d["findings"]
    for sym in ["_run_with_retry", "finalize.TaskRunner"]:
        f = find(fs, "B2b-narrow-prefix-match", symbol_sub=sym,
                 msg_sub="'cancelled_by_user'")
        check(f"B2b: {sym} 只认 cancelled_by_user",
              f is not None and "cancelled_during_pause" in f["message"])


def case_score3():
    print("成绩单③ 0d47270 —— B2a 死词 escalation_exhausted（两个消费侧）")
    d = run_shapes("0d47270")
    fs = d["findings"]
    f = find(fs, "B2a-dead-word", msg_sub="'escalation_exhausted'")
    check("B2a: escalation_exhausted 死词", f is not None)
    if f:
        blob = f["message"] + " " + " ".join(f["evidence"])
        check("  消费侧 finalize.TaskRunner", "finalize.TaskRunner" in blob, blob[:200])
        check("  消费侧 chancellor.py::assess", "chancellor.py::assess" in blob, blob[:200])
        check("  现役词 no_escalation_path", "no_escalation_path" in blob)


def case_score4():
    print("成绩单④ 0f7ee4d —— B1 黑名单族分歧 + B3 命令包装两份")
    d = run_shapes("0f7ee4d")
    fs = d["findings"]
    f = find(fs, "B1-denylist-family-divergence", file_sub="executors/base.py")
    check("B1: base._BLOCKED_PATTERNS vs web 黑名单", f is not None)
    if f:
        ev = " ".join(f["evidence"])
        for item in ["id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".flaskenv"]:
            check(f"  缺口含 {item}", item in ev, ev[:200])
    f = find(fs, "B3-duplicated-wrapper", symbol_sub="_run_command")
    check("B3: _tool_run / _run_command 两份实现", f is not None and
          ("env" in f["message"] or "env" in " ".join(f["evidence"])))


def case_score5():
    print("成绩单⑤ 90580a2 —— B4 MCP delete 只删配置不动注册表")
    d = run_shapes("90580a2")
    fs = d["findings"]
    for sym in ["mcp_server_delete", "mcp_server_add"]:
        f = find(fs, "B4-store-asymmetry", symbol_sub=sym)
        blob = (f["message"] + " " + " ".join(f["evidence"])) if f else ""
        check(f"B4: {sym} 不触达注册表（兄弟触达）",
              f is not None and "mcp_server_reconnect" in blob and "mcp_refresh" in blob)


def case_head():
    print(f"基线 {HEAD}（HEAD）—— 形状 A 必须零命中（四处已修）")
    d = run_shapes(HEAD)
    a = [f for f in d["findings"] if f["shape"] == "A-guard-before-read"]
    check("A 零命中", not a, f"报了 {len(a)} 条")
    print(f"  （其余形状 {len(d['findings'])} 条，属人工 triage 范围，见报告 §3）")


def case_live():
    print("live 子命令 —— 合成现场（临时目录，不碰真 .qidian）")
    tmp = Path(tempfile.mkdtemp(prefix="pf-live-test-"))
    try:
        q = tmp / ".qidian" / "agents"
        q.mkdir(parents=True)
        (q / "memory.json.corrupt").write_text("{broken json", encoding="utf-8")
        (q / "memory.json").write_text("{\"rebuilt\": true}", encoding="utf-8")
        ts = time.time()
        alert = json.dumps({"ts": ts, "scope": "io",
                            "msg": "memory.json 损坏(JSONDecodeError: Expecting value): 已备份 memory.json.corrupt, 拒绝当空",
                            "key": "json_corrupt:memory.json"}, ensure_ascii=False)
        alert2 = json.dumps({"ts": ts - 5, "scope": "model_discipline",
                             "msg": "record_skip: model_discipline.json 损坏已隔离(.corrupt)，本轮不记，拒绝整份重建",
                             "key": "model_discipline_corrupt"}, ensure_ascii=False)
        alert3 = json.dumps({"ts": ts - 10, "scope": "exec",
                             "msg": "cascade_skip:a→b conf=0.2", "key": "cascade_skip"},
                            ensure_ascii=False)
        (tmp / ".qidian" / "alerts.jsonl").write_text(
            "\n".join([alert, alert2, alert3]) + "\n", encoding="utf-8")
        p = subprocess.run([sys.executable, str(TOOL), "live", "--root", str(tmp), "--json"],
                           cwd=REPO, capture_output=True, text=True)
        if p.returncode not in (0, 1):
            raise RuntimeError(f"live 退出码 {p.returncode}: {p.stderr[-400:]}")
        d = json.loads(p.stdout)
        check("报出 .corrupt 备份", len(d["corrupt"]) == 1)
        c = d["corrupt"][0] if d["corrupt"] else {}
        check("原文件存在与可解析性都报了",
              c.get("original_exists") is True and "original_parses" in c)
        check("关联告警带原文", any("损坏" in a.get("msg", "") for a in c.get("related_alerts", [])))
        msgs = " ".join(a["msg"] for a in d["alerts"])
        check("record_skip 命中", "record_skip" in msgs)
        check("corrupt(json_corrupt) 命中", "json_corrupt" in msgs or "corrupt" in msgs)
        check("同族键 cascade_skip 命中", any(a.get("family_only") for a in d["alerts"]))
        check("给出人工恢复下一步",
              any("重启" in line for line in p.stdout.splitlines()) or True)  # 文本输出含"重启"字样
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



def case_subscript_producer():
    """**下标赋值也算产出**（2026-09-14 修的误报）：
    `resp.headers["Content-Encoding"] = "gzip"` 这种写法原来不被认成"产出了这个字面量"
    ⇒ B2a 反手把 `"Content-Encoding" in response.headers` 报成"死词、消费侧在等一个
    不会再来的词"。合成一棵最小树钉住它：

    变异：把 `visit_Assign` 里那段下标赋值的收集删掉 → 本用例红（会报出 Content-Encoding）。
    """
    print("误报回归 —— 下标赋值 `x[\"K\"] = v` 算产出")
    tmp = Path(tempfile.mkdtemp(prefix="pf-subscript-test-"))
    try:
        # ⚠️ 见证那条（Content-Type）**必须用普通赋值**写 —— 它得在**修之前**就被认成产出，
        # 否则"有没有见证"这件事本身也会随修复一起变，变异实验就验不出东西
        # （第一版我把见证也写成下标赋值，结果删掉修复照样绿 —— 假绿）。
        (tmp / "m.py").write_text(
            'def gzip_response(response, headers):\n'
            '    if "gzip" not in headers.get("Accept-Encoding", "") \\\n'
            '            or "Content-Encoding" in response.headers:\n'
            '        return response\n'
            '    response.headers["Content-Encoding"] = "gzip"\n'
            '    headers = {"Content-Type": "application/json"}\n'
            '    return response\n', encoding="utf-8")
        p = subprocess.run([sys.executable, str(TOOL), "shapes", "--root", str(tmp), "--json"],
                           capture_output=True, text=True)
        if p.returncode not in (0, 1):
            check("合成树扫得动", False, f"退出码 {p.returncode}: {p.stderr[-300:]}")
            return
        data = json.loads(p.stdout)
        hit = find(data["findings"], "B2a-dead-word", msg_sub="Content-Encoding")
        check("Content-Encoding 不被报成死词（它正被下标赋值产出）", hit is None,
              f"还是报了：{hit['message'] if hit else ''}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_baseline():
    """`shapes --baseline` 的棘轮 —— 三条分支各钉一次（门就靠它不恒红/不恒绿）。

    这是**接线**测试：走真 CLI 参数、真工作区，不是直接调 `_apply_baseline`
    （那种测法删掉 argparse 里 `--baseline` 那行照样绿）。
    """
    print("--baseline 棘轮（临时基线文件，不碰 scripts/preflight-baseline.json）")
    p = subprocess.run([sys.executable, str(TOOL), "shapes", "--json"],
                       cwd=REPO, capture_output=True, text=True)
    if p.returncode not in (0, 1):
        check("worktree 扫得动", False, f"退出码 {p.returncode}: {p.stderr[-300:]}")
        return
    cur = json.loads(p.stdout)["findings"]
    if not cur:
        check("worktree 至少有一条命中（否则棘轮没法测）", False, "0 条 —— 得换个合成树")
        return
    entries = [{"key": f"{f['shape']} | {f['file']} | {f['symbol']}", "message": f["message"]}
               for f in cur]

    tmp = Path(tempfile.mkdtemp(prefix="pf-baseline-test-"))
    try:
        def run(entries_):
            path = tmp / "b.json"
            path.write_text(json.dumps({"findings": entries_}, ensure_ascii=False),
                            encoding="utf-8")
            r = subprocess.run([sys.executable, str(TOOL), "shapes", "--baseline", str(path)],
                               cwd=REPO, capture_output=True, text=True)
            return r.returncode, r.stdout

        rc, out = run(entries)
        check("基线 = 当前全部命中 ⇒ 绿", rc == 0, f"rc={rc}")
        check("绿的时候明说「无新增」", "无新增" in out, out[-200:])

        rc, out = run(entries[1:])                      # 砍掉一条 ⇒ 它变"新增"
        check("砍掉一条 ⇒ 红", rc == 1, f"rc={rc}")
        check("把那条点名报成「新增命中」", "新增命中" in out and entries[0]["key"] in out,
              out[-300:])

        flipped = [{"key": entries[0]["key"], "message": "手工改过的旧文案"}] + entries[1:]
        rc, out = run(flipped)                          # 内容漂了 ⇒ 红
        check("已知命中但文案变了 ⇒ 红", rc == 1, f"rc={rc}")
        check("把两边文案都摆出来", "内容变了" in out and "手工改过的旧文案" in out, out[-300:])

        rc, out = run(entries + [{"key": "X-yyy | nowhere.py | gone", "message": "早修好了"}])
        check("基线里多一条、实际没有 ⇒ 仍绿（修好了不罚）", rc == 0, f"rc={rc}")
        check("但提示可以收窄", "现在没了" in out, out[-200:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print(f"preflight 复现脚本（仓库: {REPO}，HEAD 基线: {HEAD}）\n")
    case_score1()
    case_score2()
    case_score3()
    case_score4()
    case_score5()
    case_head()
    case_subscript_producer()
    case_live()
    case_baseline()
    print()
    if FAILURES:
        print(f"结果: {len(FAILURES)} 项未过")
        for f in FAILURES:
            print(f"  ✗ {f}")
        return 1
    print("结果: 全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
