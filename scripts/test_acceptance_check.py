#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`acceptance_check.py` 第 8 条的**路径逻辑**自测。

跑法：`.venv/bin/python scripts/test_acceptance_check.py`（或 pytest）。

**喂的是假目录，不是真 `.qidian/`** —— 真盘每天都在变，拿它当夹具会因无关轮次而红。

咬的是**真踩过的那个失败模式**（2026-09-30 `round-20260930f`）：
判分器原来在 `get_project_dir(pid)` 返回的那个**空目录**里翻报告，而报告其实是它的
**同级文件** `.qidian/projects/<id>.<名字>`；交付清单又在**第三个地方**
（`.qidian/deliverables/<id>/delivery_manifest.json`）⇒ **第 8 条恒判不过**，
而系统是好的 —— 差一点被当成"系统的洞"报出去。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("acceptance_check", _HERE / "acceptance_check.py")
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)

PID = "1790766018809"


def _mk(tmp: Path, *, qa: bool, manifest: bool) -> tuple[Path, Path]:
    pdir = tmp / "projects"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / PID).mkdir(exist_ok=True)                     # get_project_dir 返回的就是这个**空目录**
    if qa:
        (pdir / f"{PID}.qa_report.json").write_text("{}", encoding="utf-8")
        (pdir / f"{PID}.qa-report.md").write_text("# 报告", encoding="utf-8")
    dld = tmp / "deliverables" / PID
    if manifest:
        dld.mkdir(parents=True, exist_ok=True)
        (dld / "delivery_manifest.json").write_text('{"reports": ["qa_report.json"]}', encoding="utf-8")
    return pdir, dld


def _check(label: str, cond: bool) -> bool:
    print(("  ✅ " if cond else "  ❌ ") + label)
    return cond


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pdir, dld = _mk(tmp, qa=True, manifest=True)
        qa, dl = ac._find_reports(pdir, dld, PID)
        print("── 正常情况：两样都在 ──")
        ok &= _check(f"找到 QA 报告（2 份）：{qa}", len(qa) == 2)
        ok &= _check(f"找到交付清单：{dl}", dl == ["delivery_manifest.json"])

        print("── 🔴 报告在**同级文件**里，不在 `<id>/` 那个空目录里 ──")
        empty = pdir / PID
        ok &= _check("`<id>/` 确实是空的（这就是原来翻错的地方）",
                     empty.is_dir() and not list(empty.iterdir()))

        print("── 两样都没有时不许编 ──")
        p2, d2 = _mk(tmp / "b", qa=False, manifest=False)
        qa2, dl2 = ac._find_reports(p2, d2, PID)
        ok &= _check("QA 报告：空", qa2 == [])
        ok &= _check("交付清单：空", dl2 == [])

        print("── 清单目录不存在时不许炸 ──")
        qa3, dl3 = ac._find_reports(pdir, tmp / "不存在", PID)
        ok &= _check("不炸、返回空", dl3 == [])

        print("── 同名不同项目不许串 ──")
        (pdir / "其他项目.qa_report.json").write_text("{}", encoding="utf-8")
        qa4, _ = ac._find_reports(pdir, dld, PID)
        ok &= _check(f"只认本项目的：{qa4}", all(PID in n for n in qa4))

    print("── 🔴 第 4 条：`src/` 布局也要认得（round-f 就是它判不过）──")
    with tempfile.TemporaryDirectory() as td:
        w = Path(td)
        (w / "src" / "jsonlstat").mkdir(parents=True)
        (w / "src" / "jsonlstat" / "__init__.py").write_text("", encoding="utf-8")
        pkg, root = ac._find_package(w)
        ok &= _check(f"src 布局下找到包：{pkg!r}（找的是非空名，且给得出 PYTHONPATH）",
                     pkg == "jsonlstat" and root.endswith("src"))

        w2 = Path(td) / "flat"
        w2.mkdir()
        (w2 / "mypkg").mkdir()
        (w2 / "mypkg" / "__init__.py").write_text("", encoding="utf-8")
        (w2 / "tests").mkdir()
        (w2 / "tests" / "__init__.py").write_text("", encoding="utf-8")
        pkg2, root2 = ac._find_package(w2)
        ok &= _check(f"顶层布局照旧认得：{pkg2!r}（且不把 tests 当包）",
                     pkg2 == "mypkg" and root2 == "")

        pkg3, _ = ac._find_package(Path(td) / "空")
        ok &= _check("没有包时给空，不炸", pkg3 == "")

    print("\n" + ("✅ 全通过" if ok else "❌ 有失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())


def test_路径逻辑():
    assert main() == 0
