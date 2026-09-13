#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外派闭环脚手架 —— 给「往外派任务的编排 AI」减三件手工活。

子命令
------
blocklist   扫 ~/Desktop/ZCode审阅/ 与 docs/ 实际内容，生成「【硬约束·先读】**不要读**：…」段。
            格式照 docs/要发的指令-归档.md 最新一代实例（⑪⑬），行宽 90 逐字节校准过。
            要粘的段落走 stdout；扫描报告与提醒走 stderr，不污染要复制的内容。
header      生成 docs/*-2026*.md 的归档抬头骨架。五个字段全留（待填）：
            绝不代写结论、绝不出现「已核实」这种只有人核完才能写的话。
move        把某条已发出的指令从 docs/要发的指令.md 挪进 docs/要发的指令-归档.md。
            默认 dry-run：先逐字打印要改成什么、不写盘；确认后加 --apply 才写。

规矩（外派⑫）
------
· 零第三方依赖，纯标准库。
· 只读仓库；只有 `move --apply` 会写盘，且只写那两个指令文件（都在 git 里，可 diff/回滚）。
· 脚本替不了的主意（手写状态行、核实结论）绝不悄悄替你拿 —— 一律打成 stderr 提醒。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sys
from pathlib import Path

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPO = SCRIPT_DIR.parent
DEFAULT_REVIEW_DIR = HOME / "Desktop" / "ZCode审阅"
DEFAULT_PENDING = DEFAULT_REPO / "docs" / "要发的指令.md"
DEFAULT_ARCHIVE = DEFAULT_REPO / "docs" / "要发的指令-归档.md"

# 默认要挡的位置 —— 条目集合与顺序照归档实例 ⑬（最新一代紧凑式）。
# {home}/{review} 运行时替换（{review} = --review-dir，默认 ~/Desktop/ZCode审阅）。
# 审阅目录整目录挡时带注记（见 DIR_ANNOTATIONS），与实例一致。
FIXED_BLOCK_PATHS = [
    "{home}/OPEN.md",
    "{home}/Desktop/要发的指令.md",
    "{home}/Desktop/静默except分类/",
    "{review}/",
]
DIR_ANNOTATIONS = {
    "{review}/": "（**全部**）",
}
DOCS_GLOB_ANNOTATION = "（**全部归档**）"
WRAP_WIDTH = 90  # 按实例 ⑬/⑪ 校准：两份的断行位置可逐字节复现（⑫ 第二行是手写离群值）
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# ────────────────────────────── 通用小件 ──────────────────────────────


def disp_width(s: str) -> int:
    """终端显示宽度：CJK/全角记 2，其余记 1。"""
    w = 0
    for ch in s:
        o = ord(ch)
        if (
            0x2E80 <= o <= 0x9FFF      # CJK 部首/注音/汉字（含【】。，U+3000 段）
            or 0xAC00 <= o <= 0xD7A3   # 谚文
            or 0xF900 <= o <= 0xFAFF   # 兼容汉字
            or 0xFE30 <= o <= 0xFE4F   # CJK 兼容形式
            or 0xFF00 <= o <= 0xFF60   # 全角形式（：（）！等）
            or 0xFFE0 <= o <= 0xFFE6
        ):
            w += 2
        else:
            w += 1
    return w


def _sep_between(prev_entry: str) -> str:
    """条目间分隔符。全角标点收尾时不留前导空格（照实例：`（**全部**）· /Users/…`）。"""
    return "·" if prev_entry and disp_width(prev_entry[-1]) == 2 else " ·"


def render_blocklist(entries, width: int = WRAP_WIDTH) -> str:
    """把条目渲染成「【硬约束·先读】**不要读**：…」一段。

    断行规则（照 ⑬/⑪ 实例）：断在条目边界；断行时行尾留分隔点 `·`（不带尾空格）；
    单条超宽时独占一行、不再内断；最后以 `。` 收尾。
    """
    if not entries:
        raise ValueError("条目清单为空 —— 这份指令什么都不挡？先检查 --allow 是不是放行过头了")
    prefix = "【硬约束·先读】**不要读**："
    lines = []
    cur = prefix
    prev = None
    for e in entries:
        if prev is None:
            cand = cur + e
        else:
            s = _sep_between(prev)
            cand = cur + s + " " + e
            if disp_width(cand) > width:
                lines.append(cur + s)
                cur = e
                prev = e
                continue
        cur = cand
        prev = e
    lines.append(cur + "。")
    return "\n".join(lines)


def normalize_marker(arg: str) -> str:
    """⑫ / 12 两种写法都收，统一成带圈数字。"""
    if arg in CIRCLED:
        return arg
    if arg.isdigit() and 1 <= int(arg) <= 20:
        return CIRCLED[int(arg) - 1]
    raise SystemExit(f"认不出条目号：{arg!r}（要 ①~⑳ 或 1~20）")


# ────────────────────────────── 一、禁读清单 ──────────────────────────────


def scan_docs_dates(repo: Path) -> dict:
    """docs/*.md 里带 YYYYMMDD 日期后缀的，按日期归组（只看文件名，不读内容）。"""
    docs = Path(repo) / "docs"
    by_date = {}
    if docs.is_dir():
        for p in sorted(docs.glob("*.md")):
            m = re.search(r"(20\d{6})\.md$", p.name)
            if m:
                by_date.setdefault(m.group(1), []).append(p.name)
    return by_date


def build_entries(*, repo: Path, review_dir: Path, date: str, docs_days: int = 0,
                  docs_note: bool = True, allow=()):
    """算出当前该挡哪些。返回 (条目列表, stderr 提醒列表)。

    --allow 的语义（照实例：条目集合 = 默认集合 − 本任务要读的）：
      · 命中固定条目本身 → 整条不放；
      · 命中整挡目录内部的某份 → 该目录降级为逐份枚举（放行的那份不列）；
      · 命中 docs 日期 glob 范围内的某份 → 该 glob 降级为逐份枚举。
    """
    notes = []
    repo = Path(repo)
    review_dir = Path(review_dir)
    allow_n = []
    for a in allow:
        p = os.path.expanduser(str(a))
        if not os.path.isabs(p):
            p = str(repo / p)
        allow_n.append(os.path.normpath(p))

    home = str(HOME)
    entries = []
    review_n = os.path.normpath(str(review_dir))
    docs_dir = repo / "docs"
    docs_n = os.path.normpath(str(docs_dir))

    # ── 固定条目 ──
    for raw in FIXED_BLOCK_PATHS:
        p = raw.format(home=home, review=str(review_dir).rstrip("/") or "/")
        p_n = os.path.normpath(p)
        if p_n in allow_n:
            notes.append(f"已按 --allow 放行 {p} —— 记得在指令正文写明「可以读 {p}」，光不挡不够（照⑫的『可以读』段）。")
            continue
        if p_n == review_n:
            under = [a for a in allow_n if a.startswith(p_n + "/")]
            if under:
                kept = 0
                if review_dir.is_dir():
                    for kid in sorted(review_dir.iterdir()):
                        if os.path.normpath(str(kid)) in allow_n:
                            continue
                        entries.append(str(kid) + ("/" if kid.is_dir() else ""))
                        kept += 1
                notes.append(f"{p} 因 --allow 指到其内部，降级为逐份挡（{kept} 项；放行的份没列）。")
                if not review_dir.is_dir():
                    notes.append(f"⚠️ {p} 不存在或不可读 —— 枚举结果可能为空，建议人工确认。")
            else:
                ann = DIR_ANNOTATIONS.get(raw, "")
                entries.append(p + ann)
                if review_dir.is_dir():
                    n = sum(1 for _ in review_dir.iterdir())
                    notes.append(f"{p} 现有 {n} 项 → 按惯例整目录挡{ann}。")
                else:
                    notes.append(f"{p} 现在不存在 —— 照样整目录挡，防它今天长出来。")
        elif p.endswith("/"):
            # 其他目录型（静默except分类）：内容敏感，本脚本不枚举它
            if any(a.startswith(p_n + "/") for a in allow_n):
                notes.append(f"⚠️ --allow 指到 {p} 内部 —— 这个目录本脚本不枚举，请手工把其余份写成逐条。")
                continue
            entries.append(p)
        else:
            entries.append(p)

    # ── docs 日期 glob ──
    if docs_n in allow_n:
        notes.append("已按 --allow 放行整个 docs/ —— 日期 glob 全部不放。⚠️ 三思：docs 里就有归档账。")
    else:
        by_date = scan_docs_dates(repo)
        d0 = _dt.datetime.strptime(date, "%Y%m%d").date()
        covered = []
        for k in range(docs_days, -1, -1):
            d = (d0 - _dt.timedelta(days=k)).strftime("%Y%m%d")
            if k == 0 or d in by_date:
                covered.append(d)
        for d in covered:
            hits = [a for a in allow_n if a.startswith(docs_n + "/") and re.search(re.escape(d) + r"\.md$", a)]
            if hits:
                names = sorted(x.name for x in docs_dir.glob(f"*{d}.md")) if docs_dir.is_dir() else []
                kept = [f"{docs_dir}/{n}" for n in names
                        if os.path.normpath(f"{docs_dir}/{n}") not in hits]
                entries.extend(kept)
                notes.append(f"docs/*{d}.md 因 --allow 降级为逐份挡（挡 {len(kept)} 份、放行 {len(hits)} 份）—— 放行的记得在正文写明。")
            else:
                entries.append(f"{docs_dir}/*{d}.md" + (DOCS_GLOB_ANNOTATION if docs_note else ""))
            if d == date and d not in by_date:
                notes.append(f"docs/ 里 {date} 当天还没有带日期的 .md —— glob 照发，今天落进来一份就挡一份。")
        for d in sorted(by_date):
            if d not in covered:
                names = by_date[d]
                notes.append(f"docs/ 还有 {len(names)} 份 *{d}.md 不在本次挡的日期里（如 {names[0]}）—— "
                             f"若也算别的窗口的产物，加 --docs-days 或 --date 把它罩住。")
    return entries, notes


def cmd_blocklist(args) -> int:
    date = args.date or _dt.date.today().strftime("%Y%m%d")
    entries, notes = build_entries(
        repo=args.repo, review_dir=args.review_dir, date=date,
        docs_days=args.docs_days, docs_note=not args.no_docs_note, allow=args.allow,
    )
    out = render_blocklist(entries, width=args.width)
    print(out)
    if notes:
        print("[blocklist] 扫描备注（不进指令，仅供你判断）：", file=sys.stderr)
        for n in notes:
            print(f"  · {n}", file=sys.stderr)
    return 0


# ────────────────────────────── 二、归档抬头骨架 ──────────────────────────────


def build_header(*, title: str, date: str, original: str, task: str,
                 baseline: str, hard: str) -> str:
    L = [
        f"# {title}（{date}）",
        "",
        f"> **原始件路径**：{original or '（待填）'}",
        f"> **任务**：{task or '（待填）'}",
        (f"> **基线提交**：`{baseline}`" if baseline else "> **基线提交**：（待填）"),
        "> **硬约束**：" + (hard or "（待填 —— 可直接把 `dispatch.py blocklist` 的输出贴进来）"),
        ">",
        "> **我的核实**：（待填）",
        "",
        "---",
        "",
        "（原文从下面开始，一字不改；填完抬头后删掉本行）",
        "",
    ]
    return "\n".join(L)


def cmd_header(args) -> int:
    date = args.date or _dt.date.today().strftime("%Y%m%d")
    date_iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"  # 标题里照实例用 2026-09-14 式；文件名用紧凑式
    original = args.original or ""
    if original:
        original = str(Path(os.path.expanduser(original)))
        stem = Path(original).stem
        if stem.endswith(date):
            stem = stem[: -(len(date) + 1)]
        title = args.title or stem
        suggested = f"{stem}-{date}" if not stem.endswith(date) else stem
        print(f"[header] 建议归档为 docs/{suggested}.md（若已存在换 -02 之类后缀，别覆盖）。", file=sys.stderr)
    else:
        title = args.title or "（待定标题）"
    if not args.baseline:
        print("[header] 基线提交没给 —— 派发时用的那个基线，自己填（git rev-parse HEAD 是现在的，未必是当时的）。", file=sys.stderr)
    print("[header] 「我的核实」留空是有意的：核完自己写，脚本不会替你写「已核实」。", file=sys.stderr)
    print(build_header(title=title, date=date_iso, original=original,
                       task=args.task or "", baseline=args.baseline or "", hard=args.hard or ""))
    return 0


# ────────────────────────────── 三、待发 → 归档 ──────────────────────────────


def _find_item_heads(lines):
    """条目 = 围栏外、行首 `## ` 的行（围栏感知，防原文里出现 `## ` 误判）。"""
    heads, in_fence = [], False
    for i, ln in enumerate(lines):
        if ln.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and ln.startswith("## "):
            heads.append(i)
    return heads


def parse_pending(text: str):
    lines = text.split("\n")
    heads = _find_item_heads(lines)
    if not heads:
        raise ValueError("待发文件里没找到任何 `## ` 条目标题 —— 结构对吗？")
    preamble = lines[: heads[0]]
    items = []
    for k, h in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(lines)
        core = list(lines[h:end])
        while core and core[-1].strip() in ("", "---"):
            core.pop()
        items.append({"head_idx": h, "head": lines[h], "core": core,
                      "end_idx": end})
    return lines, preamble, items


def rewrite_intro_line(line: str, marker: str):
    """把抬头里 `> 🆕 **待发 N 个**：**⑫ …**（tag）· …` 的这一条摘掉、计数减一。
    返回 (新行, 是否改过)。分段格式对不上就不动（宁可留着也不瞎改）。"""
    m = re.search(r"待发\s*(\d+)\s*个", line)
    if not m:
        return line, False
    esc = re.escape(marker)
    seg = re.compile(r"\s*·?\s*\*\*" + esc + r"[^*]*\*\*(（[^）]*）)?")
    new = seg.sub("", line, count=1)
    if new == line:
        return line, False
    new = re.sub(r"(：\s*)·\s*", r"\1", new)      # 摘掉行首段后残留的 `： · `
    new = re.sub(r"·\s*·", "·", new)
    n = max(0, int(m.group(1)) - 1)
    new = re.sub(r"待发\s*\d+\s*个", f"待发 {n} 个", new, count=1)
    new = re.sub(r"：(）?)(\s*)。$", r"：——。", new)  # 全摘空后的收尾
    return new, True


def _extract_dropoff(body_lines):
    """从原文里解析【落盘】报告写到 `...` 的落点。解析不出就给占位。"""
    for ln in body_lines:
        if "【落盘】" in ln:
            m = re.search(r"报告写到\s*`([^`]+)`", ln)
            if m:
                return m.group(1), True
            m = re.search(r"`([^`]+)`", ln)
            if m:
                return m.group(1), True
            return "（落点待补）", False
    return "（落点待补）", False


def plan_move(pending_text: str, archive_text: str, marker: str, status: str = "已发出、在跑"):
    """算出 move 的完整改动计划（不写盘）。出错时返回 {'ok': False, 'error': ...}。"""
    notes = []
    p_lines, preamble, items = parse_pending(pending_text)

    target = None
    head_re = re.compile(r"^##\s*" + re.escape(marker) + r"(?:\s|$)")
    for it in items:
        if head_re.match(it["head"]):
            target = it
            break
    if target is None:
        return {"ok": False, "error": f"待发里没有 {marker} 这一条 —— 要么号写错了，要么已发过（去归档里找原文重发）。"}

    m = re.search(r"^### ~~" + re.escape(marker) + r"~~", archive_text, re.M)
    if m:
        return {"ok": False, "error": f"归档里已经有 {marker} 的条目（第 {archive_text[:m.start()].count(chr(10)) + 1} 行附近）—— "
                                      f"别重复归档；确认要强来就加 --force。"}

    title = target["head"][len("## ") + len(marker):].strip()
    body = list(target["core"][1:])
    while body and not body[0].strip():
        body.pop(0)  # 标题与围栏之间的空行不进归档条目（照归档条目的形状）
    if not body:
        return {"ok": False, "error": f"{marker} 的正文是空的 —— 没什么可归档的。"}
    if not body[0].startswith("```"):
        body = ["```"] + body + ["```"]
        notes.append("该条原文没包在围栏里，归档时补了 ``` 围栏（逐字未改）。")
    drop, drop_ok = _extract_dropoff(target["core"][1:])
    if not drop_ok:
        notes.append("原文里没解析出【落盘】报告写到 `...` 的落点 —— 抬头里落点写了占位，自己补。")

    entry_heading = f"### ~~{marker}~~ ／ {title}（**{status}** → 拟落 `{drop}`）"

    # ── 新待发：按行区间删（其余字节一律不动；被删条目和下一条之间的 --- 留给前一条当收尾）──
    in_target = set(range(target["head_idx"], target["end_idx"]))
    out = [ln for i, ln in enumerate(p_lines) if i not in in_target]
    # 展示用的行号区间（文件末尾 split 出的 phantom 空元素不算一行）
    span_hi = target["end_idx"]
    if span_hi == len(p_lines) and p_lines and p_lines[-1] == "":
        span_hi -= 1

    # 抬头「🆕 待发」行改写（抬头都在第一条之前，删行不影响其下标）
    intro_changes = []
    for i, ln in enumerate(p_lines):
        if i in in_target or "🆕" not in ln:
            continue
        new_ln, changed = rewrite_intro_line(ln, marker)
        if changed:
            intro_changes.append((i + 1, ln, new_ln))
            out[i] = new_ln
    pending_new = "\n".join(out)

    # ── 新归档 ──
    a_lines = archive_text.split("\n")
    sec = next((i for i, ln in enumerate(a_lines) if ln.startswith("## 📄")), None)
    if sec is None:
        return {"ok": False, "error": "归档里没找到 `## 📄 已发出指令的原文` 节 —— 插入点定不了，先看文件结构。"}
    in_fence = False
    end = None
    for i in range(sec + 1, len(a_lines)):
        ln = a_lines[i]
        if ln.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and ln.startswith("## "):
            end = i
            break
    if end is None:
        end = len(a_lines)
    insert_at = end
    while insert_at > sec + 1 and a_lines[insert_at - 1].strip() == "":
        insert_at -= 1
    block = ["", "---", "", entry_heading] + body + [""]
    archive_new_lines = a_lines[:insert_at] + block + a_lines[insert_at:]
    archive_new = "\n".join(archive_new_lines)

    # ── 手写状态的残留提醒（脚本不动，人来判）──
    stale = []
    for i, ln in enumerate(p_lines):
        if i in in_target or i in {c[0] - 1 for c in intro_changes}:
            continue
        if marker in ln:
            stale.append(("待发", i + 1, ln))
    for i, ln in enumerate(a_lines):
        if marker in ln:
            stale.append(("归档", i + 1, ln))
    stale.append(("待发", 0, "抬头的 🏃 在跑 / 已核完归档 行、归档的 ①账 / ②在跑 表 —— 都是手写状态，脚本不碰，发完自己对一眼。"))

    return {
        "ok": True, "marker": marker, "title": title, "drop": drop,
        "entry_heading": entry_heading, "body": body,
        "pending_old": pending_text, "pending_new": pending_new,
        "archive_old": archive_text, "archive_new": archive_new,
        "removed_head": target["head"],
        "removed_range": (target["head_idx"] + 1, span_hi),
        "insert_before_1based": end + 1,
        "intro_changes": intro_changes,
        "notes": notes, "stale": stale,
    }


def apply_move(plan, pending_path, archive_path):
    Path(pending_path).write_text(plan["pending_new"], encoding="utf-8")
    Path(archive_path).write_text(plan["archive_new"], encoding="utf-8")


def cmd_move(args) -> int:
    marker = normalize_marker(args.marker)
    p, a = Path(args.pending), Path(args.archive)
    plan = plan_move(p.read_text(encoding="utf-8"), a.read_text(encoding="utf-8"),
                     marker, status=args.status)
    if not plan["ok"]:
        print(f"[move] ✋ {plan['error']}", file=sys.stderr)
        return 1

    n_removed = plan["removed_range"][1] - plan["removed_range"][0] + 1
    print(f"== move {marker}：{'写入前预览' if args.apply else 'dry-run 预览（尚未写盘）'} ==")
    print(f"1) {p}（现 {plan['pending_old'].count(chr(10))} 行）")
    print(f"   · 删掉条目：{plan['removed_head']}")
    print(f"     （原文第 {plan['removed_range'][0]}–{plan['removed_range'][1]} 行区间共 {n_removed} 行；"
          f"正文围栏块 {len(plan['body'])} 行逐字搬走，其余行原样保留）")
    for ln_no, old, new in plan["intro_changes"]:
        print(f"   · 抬头第 {ln_no} 行改写：")
        print(f"       前：{old}")
        print(f"       后：{new}")
    print(f"   ⇒ 改后 {plan['pending_new'].count(chr(10))} 行")
    print(f"2) {a}（现 {plan['archive_old'].count(chr(10))} 行）")
    print(f"   · 在「## 📄 已发出指令的原文」节末尾（原第 {plan['insert_before_1based']} 行 `## ① …` 之前）插入：")
    print(f"       {plan['entry_heading']}")
    print(f"       （原文围栏块 {len(plan['body'])} 行，逐字照抄）")
    print(f"   ⇒ 改后 {plan['archive_new'].count(chr(10))} 行")
    if plan["notes"]:
        for n in plan["notes"]:
            print(f"   · 备注：{n}")
    print("⚠️ 手写状态脚本不碰，发完自己看一眼：")
    for where, ln_no, ln in plan["stale"]:
        loc = f"第 {ln_no} 行：" if ln_no else ""
        print(f"   · [{where}] {loc}{ln}" if ln_no else f"   · [{where}] {ln}")
    if not args.apply:
        print("—— 以上未写盘。确认无误就加 --apply 重跑。")
        return 0
    apply_move(plan, p, a)
    print("已写入。核对：git diff -- docs/要发的指令.md docs/要发的指令-归档.md；"
          "整份还原：git checkout -- <这两份>。")
    return 0


# ────────────────────────────── CLI ──────────────────────────────


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dispatch.py", description="外派闭环脚手架（见模块 docstring）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("blocklist", help="生成「【硬约束·先读】**不要读**：…」段（stdout），扫描备注走 stderr")
    b.add_argument("--repo", default=str(DEFAULT_REPO), help="仓库根（默认：脚本上级目录）")
    b.add_argument("--review-dir", default=str(DEFAULT_REVIEW_DIR), help="审阅产物目录（默认 ~/Desktop/ZCode审阅）")
    b.add_argument("--date", default="", help="docs glob 的日期 YYYYMMDD（默认今天）")
    b.add_argument("--docs-days", type=int, default=0, help="往前多挡几天有归档的日期（默认 0 = 只挡 --date 当天）")
    b.add_argument("--allow", action="append", default=[], help="本任务要读的位置（可多次）——命中即不放行/降级")
    b.add_argument("--no-docs-note", action="store_true", help="docs glob 后不带（**全部归档**）注记")
    b.add_argument("--width", type=int, default=WRAP_WIDTH, help="折行宽度（默认 90，照实例校准）")
    b.set_defaults(func=cmd_blocklist)

    h = sub.add_parser("header", help="生成归档抬头骨架（五字段全待填，stdout）")
    h.add_argument("--original", default="", help="原始件路径（也用来推导标题/建议文件名）")
    h.add_argument("--title", default="", help="文档标题（默认取原始件文件名主干）")
    h.add_argument("--task", default="", help="一句话任务（留空则待填）")
    h.add_argument("--baseline", default="", help="派发时的基线提交（留空则待填）")
    h.add_argument("--hard", default="", help="硬约束一行（留空则待填，可贴 blocklist 输出）")
    h.add_argument("--date", default="", help="归档日期 YYYYMMDD（默认今天）")
    h.set_defaults(func=cmd_header)

    m = sub.add_parser("move", help="把某条从待发挪进归档（默认 dry-run，--apply 才写盘）")
    m.add_argument("marker", help="条目号：⑫ 或 12")
    m.add_argument("--apply", action="store_true", help="真的写盘（不加就只预览）")
    m.add_argument("--status", default="已发出、在跑", help="归档抬头里的状态词（默认「已发出、在跑」）")
    m.add_argument("--pending", default=str(DEFAULT_PENDING))
    m.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    m.set_defaults(func=cmd_move)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
