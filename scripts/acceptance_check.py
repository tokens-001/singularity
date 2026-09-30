#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终点线 ③ 的判分器 —— **照 `docs/验收-端到端-20260921.md` §三逐条核，不靠印象**。

为什么是脚本：那张表的第 3 条（包干净）、第 7 条（包里测试真能跑）这类，
靠肉眼看 `tar -t` 输出**会漏**（09-17 就是漏了 7 个 `.pyc` 到收件人手里才写成判据的）。
能机械核的就别用眼睛。

覆盖：①②③④⑦⑧ 全自动；⑤⑥ 需要知道 CLI 的子命令名 ⇒ 自动发现不了时**打印 `--help`
并让人补 `--stats-cmd`**（**不猜**：猜错了会把"命令不对"读成"功能不对"）。

跑法：
  `.venv/bin/python scripts/acceptance_check.py <project_id> [--stats-cmd "python -m pkg stat --level"]`
退出码：0 = 能机械核的条全过；1 = 有不过的；2 = 参数/环境问题（**别读成"没成"**）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from singularity.scheduler import project as proj_mod          # noqa: E402

ACCEPT = Path.home() / "qidian-accept"


def _find_reports(projects_dir: Path, dl_dir: Path, pid: str) -> tuple[list[str], list[str]]:
    """第 8 条要的两样：**QA 报告**文件名 · **交付清单**文件名。

    ⚠️ **两处路径都反直觉，原版三处全找错 ⇒ 这条恒判不过，而系统其实是好的**
    （2026-09-30 `round-20260930f` 判分时抓到，差一点把它报成"系统的洞"）：

    · QA 报告是 `.qidian/projects/<id>.<名字>` —— **同级文件**，不在 `<id>/` 那个目录里
      （`get_project_dir(pid)` 返回的正是那个**空目录**）。写者：`workflow._phase_output_path`。
    · 交付清单在 `.qidian/deliverables/<id>/delivery_manifest.json` —— **不在**项目仓、
      也不在 `.qidian/projects/`。写者：`orchestrator` 那段"写交付清单"。

    抽成函数是为了**钉得住**：它错过一次，而且是"看起来像系统的洞"那种错。
    """
    qa = [f.name for f in projects_dir.glob(f"{pid}.*") if "qa" in f.name.lower()]
    dl = ([f.name for f in dl_dir.glob("delivery_manifest.json")]
          if dl_dir.exists() else [])
    return qa, dl


def _run(cmd: list[str], cwd=None, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _git(repo: Path, *args) -> str:
    return _run(["git", *args], cwd=repo).stdout or ""


class Check:
    def __init__(self, n: int, title: str):
        self.n, self.title, self.notes = n, title, []
        self.passed: bool | None = None

    def ok(self, cond: bool, note: str):
        self.notes.append(("✅ " if cond else "❌ ") + note)
        self.passed = cond if self.passed is None else (self.passed and cond)
        return cond

    def na(self, note: str):
        self.notes.append("◐ " + note)
        return True

    def report(self) -> bool:
        mark = "✅" if self.passed else ("◐" if self.passed is None else "❌")
        print(f"{mark} 第{self.n}条 {self.title}")
        for n in self.notes:
            print("     " + n)
        return bool(self.passed)


def grade(pid: str, stats_cmd: str = "") -> int:
    proj = proj_mod.load(pid)
    if proj is None:
        print(f"❗ 读不到项目 {pid}"); return 2
    repo = proj_mod.repo_dir(pid)
    checks: list[Check] = []

    # ① phase == done
    c = Check(1, "项目真走到终态"); checks.append(c)
    c.ok(str(getattr(proj.phase, "value", proj.phase)) == "done", f"phase = {proj.phase}")

    # ② 交付包出得来
    c = Check(2, "交付包真出得来"); checks.append(c)
    tags = [t for t in _git(repo, "tag", "-l", "release/*").split() if t]
    c.ok(bool(tags), f"release/* tag = {tags or '（一个都没有）'}")
    listing: list[str] = []
    if tags:
        tag = sorted(tags)[-1]
        a = _run(["git", "archive", tag], cwd=repo)
        c.ok(bool(a.stdout), f"git archive {tag} 有输出（{len(a.stdout)} 字节）")
        if a.stdout:
            with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as f:
                f.write(a.stdout.encode("utf-8", "surrogateescape"))
                tar_path = f.name
            try:
                with tarfile.open(tar_path) as tf:
                    listing = tf.getnames()
            finally:
                Path(tar_path).unlink(missing_ok=True)
            c.ok(bool(listing), f"能列出 {len(listing)} 个条目")

    # ③ 包干净
    c = Check(3, "包干净（没有 .pyc / __pycache__）"); checks.append(c)
    if listing:
        dirty = [n for n in listing if n.endswith(".pyc") or "__pycache__" in n]
        c.ok(not dirty, f"脏文件 {dirty[:5]}" if dirty else "导出清单里一个都没有")
    else:
        c.ok(False, "拿不到导出清单 ⇒ 这条没法核（**不是通过**）")

    # ④/⑤/⑥/⑦ 要在解包目录里跑
    work = None
    if listing:
        work = Path(tempfile.mkdtemp(prefix="qidian-accept-"))
        with tarfile.open(fileobj=__import__("io").BytesIO(_run(["git", "archive", sorted(tags)[-1]],
                                                               cwd=repo).stdout.encode("utf-8", "surrogateescape"))) as tf:
            tf.extractall(work)

    c = Check(4, "装得上、跑得起来"); checks.append(c)
    pkg = ""
    if work:
        cands = [p.name for p in work.iterdir()
                 if p.is_dir() and (p / "__init__.py").exists() and p.name != "tests"]
        pkg = cands[0] if cands else ""
        c.ok(bool(pkg), f"解包后找到包目录：{pkg or '（没有带 __init__.py 的一级目录）'}")
        if pkg:
            h = _run([sys.executable, "-m", pkg, "--help"], cwd=work)
            c.ok(h.returncode == 0, f"`python -m {pkg} --help` 退 {h.returncode}")
            c.notes.append("     帮助输出首行：" + (h.stdout.strip().splitlines() or [""])[0][:90])
    else:
        c.ok(False, "没解出包 ⇒ 这条没法核（**不是通过**）")

    c = Check(5, "核心功能对（对表）"); checks.append(c)
    if work and pkg and stats_cmd:
        exp = json.loads((ACCEPT / "expected.json").read_text(encoding="utf-8"))
        got = _run(stats_cmd.split() if not stats_cmd.startswith("python") else
                   [sys.executable, *stats_cmd.split()[1:]], cwd=work)
        c.notes.append(f"     命令：{stats_cmd} ⇒ 退 {got.returncode}")
        c.notes.append("     输出：" + (got.stdout or got.stderr).strip()[:300].replace("\n", " ⏎ "))
        c.na("**逐项对表要人看**（上面是原始输出，对照 "
             f"{ACCEPT/'expected.json'} 的 level_counts / source_counts / 时间窗）")
    else:
        c.na("没给 `--stats-cmd` ⇒ **不知道主统计命令叫什么**，这条留给人核（不猜）")

    c = Check(6, "边界不崩（空/全坏/文件不存在）"); checks.append(c)
    if work and pkg:
        for f, label in (("empty.jsonl", "空文件"), ("all_bad.jsonl", "全是坏行"),
                         ("nope.jsonl", "文件不存在")):
            if not stats_cmd:
                break
            r = _run([sys.executable, *stats_cmd.split()[1:], str(ACCEPT / f)], cwd=work)
            blob = (r.stdout + r.stderr)
            tb = "Traceback (most recent call last)" in blob
            c.notes.append(f"     {label}: 退 {r.returncode}，{'❌ 吐了 traceback' if tb else '没吐 traceback'} "
                           f"| {(blob.strip().splitlines() or [''])[-1][:80]}")
        if not stats_cmd:
            c.na("同上：没给 `--stats-cmd` ⇒ 留给人核")
    else:
        c.ok(False, "没解出包 ⇒ 这条没法核（**不是通过**）")

    c = Check(7, "包里的测试真能跑"); checks.append(c)
    if work:
        has_tests = any(p.name.startswith("test_") and p.suffix == ".py"
                        for p in work.rglob("test_*.py"))
        if not has_tests:
            c.na("包里没有测试文件 ⇒ **记为「不适用」并写进报告**（§三第 7 条明写不许静默跳过）")
        else:
            p = _run([sys.executable, "-m", "pytest", "-q"], cwd=work)
            c.ok(p.returncode == 0, f"解包后 pytest 退 {p.returncode}：{(p.stdout or '').strip()[-160:]}")
    else:
        c.ok(False, "没解出包 ⇒ 这条没法核（**不是通过**）")

    c = Check(8, "报告齐（交付清单 / QA 报告）"); checks.append(c)
    from singularity.scheduler import config as _cfg
    pdir = Path(proj_mod.get_project_dir(pid)).parent          # `.qidian/projects/`
    dl_dir = Path(_cfg.QIDIAN_DIR) / "deliverables" / pid
    qa, dl = _find_reports(pdir, dl_dir, pid)
    c.ok(bool(qa), f"QA 报告：{qa or '（没有）'}")
    c.ok(bool(dl), f"交付清单：{dl or '（没有）'}")
    for n in qa:
        txt = (pdir / n).read_text(encoding="utf-8", errors="ignore")
        bad = [ln for ln in txt.splitlines() if "未跑" in ln and "通过" in ln]
        c.ok(not bad, f"{n} 里没有「没跑却写通过」" + (f"：{bad[:1]}" if bad else ""))
    # 交付清单里那份"报告数"也一起看 —— 不然"报告一栏是空的"在清单里看不出来
    for n in dl:
        man = json.loads((dl_dir / n).read_text(encoding="utf-8", errors="ignore") or "{}")
        reps = man.get("reports") or []
        c.ok(bool(reps), f"{n} 的 reports 非空：{reps or '（空）'}")

    if work:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n=== ③ 判分：{proj.name}（{pid}）===")
    # ⚠️ **别写 `all(c.report() for c in checks)`** —— `all()` 会短路，
    # 第一条不过后面就**不打印**了，看着像"只核了一条"（第一版就是这么写的）。
    results = [c.report() for c in checks]
    all_ok = all(results)
    print("\n" + ("✅ 能机械核的条全过 —— **⑤ 的时间窗/计数、⑥ 的人话**还要人对着表看一遍"
                  if all_ok else "⛔ 有不过的条 —— 逐条看上面"))
    return 0 if all_ok else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id")
    ap.add_argument("--stats-cmd", default="", help="主统计命令，例如 `python -m pkg stat --level`")
    a = ap.parse_args(argv)
    return grade(a.project_id, a.stats_cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
