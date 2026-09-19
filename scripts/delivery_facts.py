#!/usr/bin/env python3
"""delivery_facts.py — 一个项目「到底发生了什么」，只摆事实，不下判词。

## 为什么存在

`~/OPEN.md` 那句「符不符合预期不知道」的根子不是没数据 —— 五样事实**全在盘上**，
只是散在五个地方，每次要看都得人肉翻 trace ＋ 侧车 ＋ 告警 ＋ bundle（2026-09-16 我翻了整轮）。

🔴 **刻意「只报事实、不下判词」**：原来那份"完全交付"的五条判据是我拟的，而且**四条在量账簿**，
真机上出现过「任务全 failed ＋ 清单三项全空，但产物完整且 40 个测试全过」——
一把尺判零交付，另一把判已交付。**所以这个脚本不替你选尺，它只把两边的读数都摆出来。**

## 用法

    .venv/bin/python scripts/delivery_facts.py 1789481895784
    .venv/bin/python scripts/delivery_facts.py --last      # 最近一个项目
    .venv/bin/python scripts/delivery_facts.py --rounds 3  # 最近 3 轮摆一起对照
    .venv/bin/python scripts/delivery_facts.py --refs      # 孤儿 pending ref（只数不删）

只读。不写任何文件、不碰状态机。
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from singularity.scheduler import config, project as proj_mod, tracker  # noqa: E402


def _j(p: Path):
    """读 json，读不到回 None —— 缺哪样就报哪样，别拿异常糊过去。"""
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _maybe_json(v):
    """这些 .json 里有字段是**字符串装着 JSON**（`project.from_dict` 存的时候就那样）。"""
    if isinstance(v, str) and v.strip()[:1] in "[{":
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _git(repo: Path, *args) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                           text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _tasks_of(pid: str) -> list:
    out = []
    d = config.QIDIAN_DIR / "tasks"
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        t = _j(f)
        if isinstance(t, dict) and t.get("project_id") == pid:
            out.append(t)
    return out


# `projects/` 里除了项目本体，还堆着一堆**侧车**（`.qa_report.json` /
# `.executable_tasks.json` / `.machine-checks.json`）—— 它们**不是项目**。
# 不做这个过滤，`--rounds` 就会把渲染出来的 QA 报告当成一轮摆上桌（`--last` 早就踩过）。
_SIDECAR_MARKS = (".qa_report", ".fusion", ".machine", ".executable")


def _project_files() -> list:
    d = config.QIDIAN_DIR / "projects"
    if not d.exists():
        return []
    # `glob` 的顺序**跟着文件系统走**（实测同一目录两次能给出不同顺序）⇒ 这里定死按文件名。
    # 文件名就是毫秒时间戳，所以名字序 == 时间序，调用方不用再排序。
    return sorted((p for p in d.glob("*.json")
                   if not any(m in p.name for m in _SIDECAR_MARKS)),
                  key=lambda p: p.name)


def _ledger_rows(pid: str) -> list:
    """这一个项目的账本行（**下界**，见 §59 —— 没有行不代表没花钱）。"""
    led = _j(config.QIDIAN_DIR / "token_usage.json")
    daily = (led or {}).get("daily") if isinstance(led, dict) else (led or [])
    return [r for r in (daily or [])
            if isinstance(r, dict) and r.get("project_id") == pid]


def _qa_verdict(pid: str) -> str:
    """一行 QA 结论。**「没有」和「读不出来」在这儿也必须分得开**（见 ② 那段）。"""
    p = config.QIDIAN_DIR / "projects" / f"{pid}.qa_report.json"
    if not p.exists():
        return "—"
    qa = _j(p)
    if qa is None:
        return "⚠️坏"
    s = _maybe_json(qa.get("summary"))
    return s.get("verdict", "?") if isinstance(s, dict) else "?"


def facts(pid: str) -> None:
    proj = proj_mod.load(pid)
    if proj is None:
        print(f"项目 {pid} 读不到（`project.load` 返回 None）")
        return
    repo = proj_mod.repo_dir(pid)

    print(f"项目 {proj.name}  ({pid})")
    print(f"  phase={getattr(proj.phase, 'value', proj.phase)}   "
          f"auto_mode={proj.auto_mode}   仓={repo}")
    print()

    # ① 任务 —— 状态机的说法
    ts = _tasks_of(pid)
    print(f"① 任务（.qidian/tasks/*.json，按 project_id 过滤）  {len(ts)} 个")
    for t in ts:
        err = (t.get("error") or "").replace("\n", " ")[:70]
        print(f"   {t.get('status','?'):8} {t.get('id','?')}  {(t.get('description') or '')[:34]:34} {err}")
    if not ts:
        print("   （一个都没有）")
    print()

    # ② QA —— 判据说法
    qa_p = config.QIDIAN_DIR / "projects" / f"{pid}.qa_report.json"
    print(f"② QA 结论（{qa_p.relative_to(config.QIDIAN_DIR.parent)}）")
    # 🔴 **"没有"和"读不出来"必须分得开**（本仓最恨的形状）：文件不在 ⇒ 压根没跑过 QA；
    # 文件在但解析不动 ⇒ 有人写坏了，**别让这两种在屏幕上长得一样**。
    if not qa_p.exists():
        print("   （文件不存在 —— 这个项目没跑过 QA）")
    elif _j(qa_p) is None:
        print("   ⚠️ 文件在，但 JSON 读不出来 —— **不是「没跑过」**，是这份报告坏了，去人工看")
    else:
        qa = _j(qa_p)
        s = _maybe_json(qa.get("summary"))
        if not isinstance(s, dict):
            print(f"   ⚠️ summary 读不出来（拿到 {type(s).__name__}）—— 原文 {str(s)[:60]!r}")
        else:
            print(f"   verdict={s.get('verdict','?')}  检查 {s.get('passed','?')}过/"
                  f"{s.get('failed','?')}败  共 {s.get('total_checks','?')}")
        iss = _maybe_json(qa.get("issues"))
        if not isinstance(iss, list):
            print(f"   ⚠️ issues 读不出来（拿到 {type(iss).__name__}）")
            iss = []
        for i in iss[:4]:
            if isinstance(i, dict):
                body = i.get("description") or i.get("detail") or i.get("suggested_fix") or ""
                print(f"   · [{i.get('severity','?'):8}] {i.get('id','?')} → {i.get('fix_route','')}")
                if body:
                    print(f"       {body.replace(chr(10), ' ')[:100]}")
        if len(iss) > 4:
            print(f"   … 还有 {len(iss) - 4} 条")
    print()

    # ③ 交付清单 —— 账本的说法（**"零交付"这个印象就是从这一行来的**）
    man_p = config.QIDIAN_DIR / "deliverables" / pid / "delivery_manifest.json"
    print(f"③ 交付清单（{man_p.relative_to(config.QIDIAN_DIR.parent)}）")
    if not man_p.exists():
        print("   （文件不存在 —— 这个项目没走到交付那一步）")
    elif _j(man_p) is None:
        print("   ⚠️ 文件在，但 JSON 读不出来 —— **不是「没交付」**，是这份清单坏了")
    else:
        man = _j(man_p)
        print(f"   code_ref={man.get('code_ref','?')}")
        for k in ("artifacts", "docs", "reports"):
            v = man.get(k) or []
            print(f"   {k:10} {len(v)} 条" + (f"  {v[:2]}" if v else "   ← 空"))
    print()

    # ④ 产物 —— **磁盘的说法**（唯一不经过账本的）
    print(f"④ 产物（真看仓库 {repo}）")
    if not repo.exists():
        print("   （项目仓不存在）")
    else:
        # 过滤掉构建/系统垃圾 —— 它们不是交付物，混进来会把"产物"那一栏撑成假的
        _junk = {".DS_Store", ".gitignore", ".gitattributes"}
        _junk_dirs = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}
        files = [p for p in sorted(repo.rglob("*"))
                 if p.is_file() and not (_junk_dirs & set(p.parts))
                 and p.name not in _junk and not p.name.endswith(".pyc")]
        for p in files[:8]:
            print(f"   {p.stat().st_size:>8,}B  {p.relative_to(repo)}")
        if not files:
            print("   （仓里没有任何文件）")
        tags = _git(repo, "tag", "-l", "release/*").split()
        merges = [l for l in _git(repo, "log", "--oneline", "--merges").splitlines()]
        pending = _git(repo, "for-each-ref", "--format=%(refname) %(objectname:short)",
                       "refs/qidian/pending/").splitlines()
        print(f"   release 标签 {len(tags)} 个" + (f"  最近 {tags[-1]}" if tags else "   ← 没有"))
        print(f"   合并提交 {len(merges)} 条" + (f"  最近 {merges[0][:50]}" if merges else ""))
        print(f"   pending ref {len(pending)} 条" + (f"  {pending[0][:60]}" if pending else ""))
    print()

    # ⑤ 账本 —— 花销的说法
    rows = _ledger_rows(pid)
    print("⑤ 账本（.qidian/token_usage.json）")
    if rows:
        tot = sum(r.get("tokens", 0) or 0 for r in rows)
        models = sorted({r.get("model", "?") for r in rows})
        print(f"   {len(rows)} 行 · {tot:,} tokens · 模型 {', '.join(models)}")
    else:
        print("   （没有这个项目的账 —— 不代表没花钱，见 §59：记账是下界）")
    # ⑥ 集成 —— **进仓 / 没进仓**（项目详情里「集成实况」那块，从命令行也能读）
    #
    # 2026-09-20 加。为什么值得单列：这一栏原来只在界面上，而**判下一轮改动有没有变好**
    # 要的正是这个数 —— 命令行能读才谈得上"可机检"。
    # 🔴 **两个方向一起看才算数**：
    #   · `not_merged` 落下来 —— ref 只在**真有改动**时才打（`_anchor_ref` 的前提是
    #     `branch_ref != snapshot_ref`）⇒ 它量的是**真产物**；
    #   · `merged.files` 里得出现产物文件名 —— `merged.insertions` **不能单独看**：
    #     真机实测过「进仓 478 行」**全是 README / pyproject 样板**、产物一行没进树，
    #     而那个数字看着像好消息。**数字必须连着组成一起读**。
    print("⑥ 集成（进仓 / 没进仓）")
    try:
        from singularity.scheduler._api_projects import project_integration
        ig = project_integration(proj)
        mg, nm = ig["merged"], ig["not_merged"]
        print(f"   进仓   {len(mg['tasks'])} 个任务 · {mg['commits']} 个提交 · "
              f"+{mg['insertions']} 行 · 文件 {mg['files'] or '（无）'}")
        print(f"   没进仓 {len(nm['tasks'])} 个任务 · +{nm['insertions']} 行 "
              f"（各任务合计、有重复）—— 锚还在 refs/qidian/pending/")
        print(f"   集成测试：{ig['tests_ran']}（None = 没记录，不是没跑）"
              f" · 机械检查 {ig['machine_checks']}")
    except Exception as e:
        print(f"   ⚠️ 读不出来：{type(e).__name__}: {e}")
    print("\n（以上都是事实，判不判「成了」由你定。→ 来源路径都印在上面的括号里）")


def _round_lines(proj) -> list:
    """一轮 = 两行。第 1 行是身份，第 2 行是**固定顺序**的事实 —— 顺序固定是为了让几轮
    叠着看时同一件东西落在同一列上（"摆在一起"要的正是这个）。"""
    ts = _tasks_of(proj.id)
    by_status = {}
    for t in ts:
        k = t.get("status", "?")
        by_status[k] = by_status.get(k, 0) + 1
    n_ids = len(proj.task_ids or [])
    head = (f"  {proj.id}  {getattr(proj.phase, 'value', proj.phase):<10} "
            f"任务 {len(ts)} 个")
    if by_status:
        head += "（" + " · ".join(f"{k} {v}" for k, v in sorted(by_status.items())) + "）"
    head += f" · QA={_qa_verdict(proj.id)}"

    try:
        from singularity.scheduler._api_projects import project_integration
        ig = project_integration(proj)
        mg, nm = ig["merged"], ig["not_merged"]
        files = mg["files"]
        shown = ", ".join(files[:2]) + (f" …+{len(files) - 2}" if len(files) > 2 else "")
        # 🔴 **两栏会重叠**，而且屏幕上看着像 `2 + 7 = 9` 的干净划分 —— **2026-09-20 实测抓到**：
        # 真机任务 `1789836336312` 前一次尝试只交了 `pyproject.toml +36`（**这笔进了 HEAD**），
        # 一小时后的那次交了 `jsonlstat/*.py` 八个文件 `+1217 行`（**锚在 ref 上没进仓**）
        # ⇒ 同一个任务两栏都在。**重叠是正常的**（`169081e` 起"被取代"不再提前松锚），
        # 但**读者把两栏相加 = 假的全覆盖**（同族：计数判据掩盖缺口）。
        # 所以这个数**每轮都印**（哪怕是 0）—— 有条件的字段会破坏"几轮叠着看对齐"这件事。
        both = sorted(set(mg["tasks"]) & set(nm["tasks"]))
        body = (f"     进仓 {len(mg['tasks'])}/{n_ids} 任务 · {mg['commits']} 提交 · "
                f"+{mg['insertions']} 行 · 文件 {shown or '（无）'}"
                f"   |   没进仓 {len(nm['tasks'])} 任务 · +{nm['insertions']} 行"
                f" · 两栏都在 {len(both)} 个"
                + (f"（{', '.join(both)}）" if both else ""))
    except Exception as e:
        body = f"     ⚠️ 集成读不出来：{type(e).__name__}: {e}"

    rows = _ledger_rows(proj.id)
    tok = f"{sum(r.get('tokens', 0) or 0 for r in rows):,} tokens" if rows else "无账（下界）"
    body += f"   |   {tok}"
    return [head, body]


def rounds_table(limit: int | None = None) -> None:
    """**跨轮次对照** —— 把最近几轮摆在一起，看「这一轮比上一轮好没好」。

    来历（`docs/外派评审-20260920.md` §九）：单轮的事实已经能读了，但**"摆在一起"那一下没有**
    —— 而外派给的那个靶子（「三轮内 6/11 → 9/11，且单轮 token 下降」）要的正是这个。

    🔴 **这一栏存在的唯一理由，是那把假尺子**：真机上出现过「进仓 +478 行」——看着像好消息，
    拆开一看**全是 README / pyproject 样板**，产物一行没进树。所以每轮都印**文件名**，
    行数**从不单独出现**。
    """
    pids = [p.stem for p in sorted(_project_files())]   # id 就是毫秒时间戳 ⇒ 字典序 == 时间序
    if limit:
        pids = pids[-limit:]
    if not pids:
        print("没有项目"); return
    print(f"跨轮次对照（{len(pids)} 轮，按创建时刻，越靠下越新）")
    print("  `进仓` = 真躺在项目仓 HEAD 树上的产物 —— **不是**「提交在不在历史里」；"
          "行数一律连着文件名读。")
    print()
    for pid in pids:
        proj = proj_mod.load(pid)
        if proj is None:
            print(f"  {pid}  ⚠️ project.load 返回 None（文件在但读不出来）")
            continue
        for line in _round_lines(proj):
            print(line)
    print()
    print("  ⚠️ 「没进仓」行数是各锚相对各自父提交的合计、**含重复**（多任务改同一文件）"
          "—— 要的是量级，不是精确值。")


def orphan_refs_report() -> None:
    """**孤儿 pending ref** —— 只数不删（定义写在 `_api_tasks.orphan_refs` 的 docstring 里）。

    为什么它值得单开一条：这些 ref 的产物**还在**，而界面上**一个字都看不见**
    （`salvageable_refs()` 那张表是**按 task_id 挂到任务行上**的，
    任务文件没了的那些**没有行能挂**）—— 本仓的老形状，"盘上有一份、界面上看不见"。
    """
    from singularity.scheduler._api_tasks import salvageable_refs, orphan_refs
    allr = salvageable_refs()
    orph = orphan_refs()
    print("⑥ 孤儿 pending ref（产物还在、**任务文件已经没了**）")
    print(f"   pending ref {len(allr)} 条 · 其中**孤儿 {len(orph)} 条** "
          f"（另有 {len(allr) - len(orph)} 条挂得上任务，在任务页看得到）")
    for tid, sha in sorted(orph.items()):
        print(f"   · {tid}  → {sha[:7]}   （⚠️ 它是什么，已经查不到了 —— 任务文件没了）")
    if orph:
        print("   ⚠️ **别自动清**：清一条 = 永久删掉一份**还在**的产物，而"
              "「该不该留」的判据（任务文件）已经没了 ⇒ 只能人判。")
        print("   ⚠️ **扫不到的盲区**：项目仓自己被删掉时，里面的 ref 跟着没了，这里数不出来。")
    print()


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--refs":
        orphan_refs_report()
        return 0
    if args[0] == "--rounds":
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
        rounds_table(limit)
        return 0
    if args[0] == "--last":
        cands = sorted(_project_files(), key=lambda p: p.stat().st_mtime)
        if not cands:
            print("没有项目"); return 1
        pid = cands[-1].stem
    else:
        pid = args[0]
    facts(pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
