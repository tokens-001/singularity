#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dispatch.py 的测试。跑法：python3 scripts/test_dispatch.py（或 pytest scripts/test_dispatch.py）。

夹具的形状照 docs/要发的指令.md / docs/要发的指令-归档.md 的真实结构缩微：
抬头（🆕 待发行）+ `## ⑬ 标题` + 围栏原文 + `---` 分隔；归档 = 📄 原文节 + ① 账表。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import dispatch as D  # noqa: E402

FENCE = "```"
DISPATCH = SCRIPTS / "dispatch.py"

PENDING = """# 要发的指令（**只装「还没发」的**）

> 🆕 **待发 2 个**：**⑬ 审甲**（只读）· **⑫ 写乙**（写脚本）。
> 🏃 **在跑 0 个**。

---

**下一条要发什么**：⑬ → ⑫。

## ⑬ 审甲（只读）

__FENCE__
【硬约束·先读】**不要读**：x。

【落盘】报告写到 `~/Desktop/ZCode审阅/审甲-01.md`。
__FENCE__

---

## ⑫ 写乙（写脚本）

__FENCE__
【硬约束·先读】**不要读**：y。

【落盘】报告写到 `~/Desktop/ZCode审阅/写乙-01.md`。
__FENCE__

---

""".replace("__FENCE__", FENCE)

ARCHIVE = """# 发过的指令原文 + 归档账

> 这份只装已经发出去的。

---

## 📄 已发出指令的原文（按需复制）

> 下面这些都已经发出去了。

---

### ~~⑪~~ ／ 预检工具（**已发出、在跑** → 拟落 `~/Desktop/ZCode审阅/预检工具-01.md`）
__FENCE__
（⑪ 的原文）
__FENCE__

## ① 已经跑完并核完归档的

| 事项 | 状态 |
|---|---|
| 占位 | 占位 |
""".replace("__FENCE__", FENCE)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestRenderBlocklist(unittest.TestCase):
    def test_matches_instance_13_byte_for_byte(self):
        """默认条目集的渲染必须和归档实例 ⑬ 的三行逐字节一致（校准的锚）。"""
        entries = [
            f"{D.HOME}/OPEN.md",
            f"{D.HOME}/Desktop/要发的指令.md",
            f"{D.HOME}/Desktop/静默except分类/",
            f"{D.HOME}/Desktop/ZCode审阅/（**全部**）",
            f"{D.HOME}/projects/singularity/docs/*20260914.md（**全部归档**）",
        ]
        expected = "\n".join([
            f"【硬约束·先读】**不要读**：{D.HOME}/OPEN.md · {D.HOME}/Desktop/要发的指令.md ·",
            f"{D.HOME}/Desktop/静默except分类/ · {D.HOME}/Desktop/ZCode审阅/（**全部**）·",
            f"{D.HOME}/projects/singularity/docs/*20260914.md（**全部归档**）。",
        ])
        self.assertEqual(D.render_blocklist(entries), expected)

    def test_long_entry_gets_its_own_line(self):
        """单条超宽时独占一行、不内断（照实例 ⑪ 里 __rewrite_sample__ 那行的行为）。"""
        long_path = f"{D.HOME}/projects/singularity/src/singularity/web/frontend/src/pages/__rewrite_sample__/"
        self.assertGreater(D.disp_width(long_path), D.WRAP_WIDTH)
        out = D.render_blocklist([f"{D.HOME}/OPEN.md", long_path])
        last = out.splitlines()[-1]
        self.assertEqual(last, long_path + "。")

    def test_empty_entries_raises(self):
        with self.assertRaises(ValueError):
            D.render_blocklist([])

    def test_fullwidth_tail_separator_has_no_leading_space(self):
        out = D.render_blocklist(["（**全部**）", "short/path.md"])
        self.assertIn("（**全部**）· short/path.md", out)  # 全角收尾后分隔点不空格（照实例）
        out2 = D.render_blocklist(["a.md", "b.md"])
        self.assertIn("a.md · b.md", out2)  # ASCII 收尾则是普通「 · 」


class TestBuildEntries(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.review = self.root / "review"
        self.review.mkdir()
        self.repo = self.root / "repo"
        (self.repo / "docs").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_set_and_order(self):
        _write(self.repo / "docs" / "扫bug-01-20260914.md", "x")
        entries, notes = D.build_entries(repo=self.repo, review_dir=self.review, date="20260914")
        self.assertEqual(entries, [
            f"{D.HOME}/OPEN.md",
            f"{D.HOME}/Desktop/要发的指令.md",
            f"{D.HOME}/Desktop/静默except分类/",
            f"{self.review}/（**全部**）",
            f"{self.repo}/docs/*20260914.md（**全部归档**）",
        ])

    def test_default_review_dir_uses_real_zcode_path(self):
        entries, _ = D.build_entries(repo=self.repo, review_dir=D.DEFAULT_REVIEW_DIR, date="20260914")
        self.assertIn(f"{D.HOME}/Desktop/ZCode审阅/（**全部**）", entries)

    def test_allow_fixed_file_omits_entry(self):
        entries, _ = D.build_entries(repo=self.repo, review_dir=self.review, date="20260914",
                                     allow=[f"{D.HOME}/OPEN.md"])
        self.assertNotIn(f"{D.HOME}/OPEN.md", entries)
        self.assertIn(f"{D.HOME}/Desktop/静默except分类/", entries)

    def test_allow_inside_review_dir_enumerates_siblings(self):
        _write(self.review / "a.md", "x")
        _write(self.review / "b.md", "y")
        (self.review / "sub").mkdir()
        entries, notes = D.build_entries(repo=self.repo, review_dir=self.review, date="20260914",
                                         allow=[str(self.review / "a.md")])
        self.assertNotIn(f"{self.review}/（**全部**）", entries)
        self.assertIn(f"{self.review}/b.md", entries)
        self.assertIn(f"{self.review}/sub/", entries)
        self.assertNotIn(f"{self.review}/a.md", entries)
        self.assertTrue(any("逐份" in n for n in notes))

    def test_allow_inside_docs_glob_enumerates_rest(self):
        _write(self.repo / "docs" / "x-20260914.md", "x")
        _write(self.repo / "docs" / "y-20260914.md", "y")
        entries, notes = D.build_entries(repo=self.repo, review_dir=self.review, date="20260914",
                                         allow=[str(self.repo / "docs" / "y-20260914.md")])
        self.assertNotIn(f"{self.repo}/docs/*20260914.md（**全部归档**）", entries)
        self.assertIn(f"{self.repo}/docs/x-20260914.md", entries)
        self.assertNotIn(f"{self.repo}/docs/y-20260914.md", entries)

    def test_older_uncovered_date_is_flagged(self):
        _write(self.repo / "docs" / "旧答卷-20260913.md", "x")
        _, notes = D.build_entries(repo=self.repo, review_dir=self.review, date="20260914")
        self.assertTrue(any("20260913" in n for n in notes))

    def test_docs_days_covers_yesterday(self):
        _write(self.repo / "docs" / "旧答卷-20260913.md", "x")
        entries, _ = D.build_entries(repo=self.repo, review_dir=self.review,
                                     date="20260914", docs_days=1)
        self.assertIn(f"{self.repo}/docs/*20260913.md（**全部归档**）", entries)


class TestHeader(unittest.TestCase):
    def test_skeleton_has_five_fields_and_no_claims(self):
        out = D.build_header(title="审测试判据-01", date="20260914",
                             original="/tmp/审测试判据-01.md", task="审测试判据",
                             baseline="9d0d389", hard="")
        for label in ("原始件路径", "任务", "基线提交", "硬约束", "我的核实"):
            self.assertIn(label, out)
        self.assertIn("`9d0d389`", out)
        self.assertIn("（待填）", out)
        # 绝不代写结论
        self.assertNotIn("已核实", out)
        self.assertNotIn("已验证", out)
        self.assertNotIn("全真", out)

    def test_missing_fields_stay_blank(self):
        out = D.build_header(title="（待定标题）", date="20260914", original="",
                             task="", baseline="", hard="")
        self.assertEqual(out.count("（待填）"), 4)


class TestMove(unittest.TestCase):
    def test_parse_roundtrip_is_identity(self):
        lines, _, items = D.parse_pending(PENDING)
        self.assertEqual("\n".join(lines), PENDING)
        self.assertEqual([it["head"] for it in items], ["## ⑬ 审甲（只读）", "## ⑫ 写乙（写脚本）"])

    def test_plan_moves_item_and_updates_intro(self):
        plan = D.plan_move(PENDING, ARCHIVE, "⑫")
        self.assertTrue(plan["ok"], plan.get("error"))
        # 待发：⑫ 整块没了，⑫ 的抬头分段也没了，计数减一；⑬ 原样
        self.assertNotIn("## ⑫", plan["pending_new"])
        self.assertNotIn("**⑫", plan["pending_new"])
        self.assertIn("**待发 1 个**：**⑬ 审甲**（只读）。", plan["pending_new"])
        self.assertIn("## ⑬ 审甲（只读）", plan["pending_new"])
        self.assertIn("【硬约束·先读】**不要读**：x。", plan["pending_new"])
        # 归档：条目插在 📄 节内、① 账表之前；落点从【落盘】解析
        entry = "### ~~⑫~~ ／ 写乙（写脚本）（**已发出、在跑** → 拟落 `~/Desktop/ZCode审阅/写乙-01.md`）"
        self.assertIn(entry, plan["archive_new"])
        self.assertLess(plan["archive_new"].index(entry), plan["archive_new"].index("## ① "))
        self.assertIn("【硬约束·先读】**不要读**：y。", plan["archive_new"])
        self.assertNotIn("```\n```", plan["archive_new"])  # 原文有围栏就原样用，不许套双层
        # ⑪ 的原条目没被碰
        self.assertIn("### ~~⑪~~ ／ 预检工具", plan["archive_new"])

    def test_apply_writes_and_refuses_repeat(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "pending.md", PENDING)
            a = _write(Path(td) / "archive.md", ARCHIVE)
            plan = D.plan_move(p.read_text(encoding="utf-8"), a.read_text(encoding="utf-8"), "⑫")
            D.apply_move(plan, p, a)
            self.assertNotIn("## ⑫", p.read_text(encoding="utf-8"))
            self.assertIn("### ~~⑫~~", a.read_text(encoding="utf-8"))
            # 再挪一次要被拒（防重复归档）
            plan2 = D.plan_move(p.read_text(encoding="utf-8"), a.read_text(encoding="utf-8"), "⑫")
            self.assertFalse(plan2["ok"])
            self.assertIn("待发里没有", plan2["error"])

    def test_missing_marker_errors(self):
        plan = D.plan_move(PENDING, ARCHIVE, "⑮")
        self.assertFalse(plan["ok"])

    def test_already_archived_errors(self):
        archived = ARCHIVE.replace("### ~~⑪~~", "### ~~⑫~~")
        plan = D.plan_move(PENDING, archived, "⑫")
        self.assertFalse(plan["ok"])
        self.assertIn("归档里已经有", plan["error"])

    def test_dry_run_cli_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "pending.md", PENDING)
            a = _write(Path(td) / "archive.md", ARCHIVE)
            before = (p.read_text(encoding="utf-8"), a.read_text(encoding="utf-8"))
            r = subprocess.run([sys.executable, str(DISPATCH), "move", "⑫",
                                "--pending", str(p), "--archive", str(a)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("dry-run", r.stdout)
            self.assertEqual((p.read_text(encoding="utf-8"), a.read_text(encoding="utf-8")), before)

    def test_cli_apply_moves(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "pending.md", PENDING)
            a = _write(Path(td) / "archive.md", ARCHIVE)
            r = subprocess.run([sys.executable, str(DISPATCH), "move", "12",
                                "--apply", "--pending", str(p), "--archive", str(a)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("## ⑫", p.read_text(encoding="utf-8"))
            self.assertIn("### ~~⑫~~ ／ 写乙（写脚本）", a.read_text(encoding="utf-8"))


class TestBlocklistCli(unittest.TestCase):
    def test_stdout_is_paste_ready_and_stderr_carries_notes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "docs").mkdir()
            review = root / "review"
            review.mkdir()
            _write(review / "某报告.md", "x")
            r = subprocess.run([sys.executable, str(DISPATCH), "blocklist",
                                "--repo", str(root), "--review-dir", str(review),
                                "--date", "20260914"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(r.stdout.startswith("【硬约束·先读】**不要读**："))
            self.assertTrue(r.stdout.endswith("。\n"))
            self.assertIn(str(review) + "/（**全部**）", r.stdout)
            self.assertIn("现有 1 项", r.stderr)


if __name__ == "__main__":
    unittest.main()
