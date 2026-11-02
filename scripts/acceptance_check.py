#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终点线 ③ 的判分器 —— **照 `docs/验收-端到端-20260921.md` §三逐条核，不靠印象**。

为什么是脚本：那张表的第 3 条（包干净）、第 7 条（包里测试真能跑）这类，
靠肉眼看 `tar -t` 输出**会漏**（09-17 就是漏了 7 个 `.pyc` 到收件人手里才写成判据的）。
能机械核的就别用眼睛。

覆盖：①②③④⑦⑧ 全自动；⑤⑥ 需要知道 CLI 的子命令名 ⇒ 自动发现不了时**打印 `--help`
并让人补 `--stats-cmd`**（**不猜**：猜错了会把"命令不对"读成"功能不对"）。

**2026-11-02 起按 §七的 80 分制计分**：8 条各 12.5 分，总分 ≥80 算 ③ 达成。
⑤⑥ 机械核不了 ⇒ 用 `--manual` 给人给的分；失败任务用 `--unknown-failures` 报
（**不填按最坏算**，上限 79 分）。

跑法：
  `.venv/bin/python scripts/acceptance_check.py <project_id> \
      [--stats-cmd "..."] [--manual 5=1,6=2/3] [--unknown-failures 0]`
退出码：0 = 总分 ≥80；1 = 没到；2 = 参数/环境问题（**别读成"没成"**）。
"""
from __future__ import annotations

import argparse
import json
import os
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


def _run(cmd: list[str], cwd=None, timeout=120, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


def _find_package(work: Path) -> tuple[str, str]:
    """解包目录里找包 ⇒ `(包名, 要设进 PYTHONPATH 的目录)`；找不到给 `("", "")`。

    ⚠️ **`src/` 布局是标准做法，只看顶层会把好好的包判成"没找到"**
    （2026-09-30 `round-20260930f` 就是：包在 `src/jsonlstat/`，
    判分器报"没有带 `__init__.py` 的一级目录"⇒ 第 4/6 条**双双判不过**，
    而那是**判分器的眼睛问题**，不是包的毛病）。
    """
    def _pkgs(root: Path) -> list[str]:
        if not root.is_dir():
            return []
        return [p.name for p in root.iterdir()
                if p.is_dir() and (p / "__init__.py").exists() and p.name != "tests"]

    top = _pkgs(work)
    if top:
        return top[0], ""
    src = work / "src"
    under_src = _pkgs(src)
    if under_src:
        return under_src[0], str(src)        # `python -m` 要 import 得到它
    return "", ""


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

    def pending(self, note: str):
        """要人判分（⑤⑥）—— **不是通过**，这一条不打分，等 `--manual`。"""
        self.notes.append("◐ " + note)
        return True

    def skipped(self, note: str):
        """判据明确允许的「不适用」—— 按满分记（§三 第 7 条）。"""
        self.notes.append("◐ " + note)
        self.passed = True
        return True

    def score(self, manual: float | None = None) -> float:
        """0~1。⑤⑥ 只看人给的分；其余看机械判定（没判定 ⇒ 0，不许默认通过）。"""
        if self.n in (5, 6):
            return 0.0 if manual is None else max(0.0, min(1.0, manual))
        return 1.0 if self.passed else 0.0

    def report(self, score: float) -> bool:
        mark = "✅" if score >= 1 else ("❌" if score <= 0 else "◐")
        print(f"{mark} 第{self.n}条 {self.title}  [{score * 12.5:.1f}/12.5]")
        for n in self.notes:
            print("     " + n)
        return score >= 1


def _total(checks: list[Check], manual: dict[int, float] | None = None) -> float:
    """8 条各 12.5 分（§七）。抽出来是为了让计分逻辑能被单测钉住。"""
    m = manual or {}
    return sum(c.score(m.get(c.n)) for c in checks) * 12.5


def _final_score(checks: list[Check], manual: dict[int, float] | None = None,
                 unknown_failures: int | None = None) -> float:
    """最终分：**先舍 2 位**（0.6×12.5 这类浮点会落到 79.99…），再按未知失败封顶 79。"""
    total = round(_total(checks, manual), 2)
    if unknown_failures is None or unknown_failures:
        total = min(total, 79.0)
    return total


def grade(pid: str, stats_cmd: str = "", manual: dict[int, float] | None = None,
          unknown_failures: int | None = None) -> int:
    manual = manual or {}
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
        pkg, pkg_root = _find_package(work)
        c.ok(bool(pkg), f"解包后找到包目录：{pkg or '（没有带 __init__.py 的一级目录）'}"
                        + (f"（在 src/ 下）" if pkg and pkg_root else ""))
        if pkg:
            env = dict(os.environ)
            if pkg_root:
                env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
            h = _run([sys.executable, "-m", pkg, "--help"], cwd=work, env=env)
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
        c.pending("**逐项对表要人看**（上面是原始输出，对照 "
                  f"{ACCEPT/'expected.json'} 的 level_counts / source_counts / 时间窗）")
    else:
        c.pending("没给 `--stats-cmd` ⇒ **不知道主统计命令叫什么**，这条留给人核（不猜）")

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
            c.pending("同上：没给 `--stats-cmd` ⇒ 留给人核")
    else:
        c.ok(False, "没解出包 ⇒ 这条没法核（**不是通过**）")

    c = Check(7, "包里的测试真能跑"); checks.append(c)
    if work:
        has_tests = any(p.name.startswith("test_") and p.suffix == ".py"
                        for p in work.rglob("test_*.py"))
        if not has_tests:
            c.skipped("包里没有测试文件 ⇒ **记为「不适用」并写进报告**（§三第 7 条明写不许静默跳过）")
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
    missing = [n for n in (5, 6) if n not in manual]
    if missing:
        print(f"⚠️ 第 {'/'.join(map(str, missing))} 条没给 `--manual` ⇒ 按 0 分算（机械核不了）")
    # ⚠️ **别写 `all(c.report() for c in checks)`** —— `all()` 会短路，
    # 第一条不过后面就**不打印**了，看着像"只核了一条"（第一版就是这么写的）。
    scores = [c.score(manual.get(c.n)) for c in checks]
    for c, s in zip(checks, scores, strict=True):
        c.report(s)
    if unknown_failures is None:
        print("\n⚠️ 没给 `--unknown-failures` ⇒ 按最坏算（有未查清的失败），上限 79 分")
    total = _final_score(checks, manual, unknown_failures)
    ok = total >= 80
    print(f"\n总分：{total:.1f}/100（达标线 80）" + ("  ✅ ③ 达成" if ok else "  ⛔ 未达成"))
    return 0 if ok else 1


def _manual(s: str) -> dict[int, float]:
    """`5=0.67,6=2/3` ⇒ `{5: 0.67, 6: 0.666…}`。只收 ⑤⑥（其余条机械判）。"""
    out: dict[int, float] = {}
    for part in filter(None, (p.strip() for p in s.split(","))):
        k, _, v = part.partition("=")
        if not k.isdigit() or not v:
            raise argparse.ArgumentTypeError(f"看不懂的分数：{part}（写成 5=0.67 这样）")
        n = int(k)
        if n not in (5, 6):
            raise argparse.ArgumentTypeError("--manual 只接受 5 / 6（这两条要人判）")
        try:
            val = float(v.split("/")[0]) / float(v.split("/")[1]) if "/" in v else float(v)
        except (ValueError, ZeroDivisionError, IndexError):
            raise argparse.ArgumentTypeError(f"看不懂的分数：{part}（写 0~1 或 2/3）")
        out[n] = max(0.0, min(1.0, val))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id")
    ap.add_argument("--stats-cmd", default="", help="主统计命令，例如 `python -m pkg stat --level`")
    ap.add_argument("--manual", type=_manual, default={},
                    help="⑤⑥ 的人判分：`5=0.67,6=2/3`（0~1）")
    ap.add_argument("--unknown-failures", type=int, default=None,
                    help="本轮未查清的失败任务数；不填按最坏算（上限 79 分）")
    a = ap.parse_args(argv)
    return grade(a.project_id, a.stats_cmd, a.manual, a.unknown_failures)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
