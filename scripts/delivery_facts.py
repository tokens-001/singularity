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
    led = _j(config.QIDIAN_DIR / "token_usage.json")
    rows = [r for r in ((led or {}).get("daily") if isinstance(led, dict) else (led or []))
            if isinstance(r, dict) and r.get("project_id") == pid]
    print("⑤ 账本（.qidian/token_usage.json）")
    if rows:
        tot = sum(r.get("tokens", 0) or 0 for r in rows)
        models = sorted({r.get("model", "?") for r in rows})
        print(f"   {len(rows)} 行 · {tot:,} tokens · 模型 {', '.join(models)}")
    else:
        print("   （没有这个项目的账 —— 不代表没花钱，见 §59：记账是下界）")
    print("\n（以上都是事实，判不判「成了」由你定。→ 来源路径都印在上面的括号里）")


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--last":
        ps = sorted((config.QIDIAN_DIR / "projects").glob("*.json"),
                    key=lambda p: p.stat().st_mtime) if (config.QIDIAN_DIR / "projects").exists() else []
        cands = [p for p in ps if ".qa_report" not in p.name and ".fusion" not in p.name
                 and ".machine" not in p.name and ".executable" not in p.name]
        if not cands:
            print("没有项目"); return 1
        pid = cands[-1].stem
    else:
        pid = args[0]
    facts(pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
