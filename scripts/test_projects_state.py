#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""projects_state.py 的自测。跑法：`.venv/bin/python scripts/test_projects_state.py`（或 pytest）。

**喂的是假项目 / 假任务，不是真 `.qidian/`** —— 真盘每天都在变，拿它当夹具的话，
测试会因为无关的轮次而红。

每条对着一个**会真实误导人**的失败模式：
  · 咬 ①（这条是真踩过的）：`str(TaskStatus.DONE)` 是 `'TaskStatus.DONE'` ⇒
    直接拿它比终态集合会把**所有终态任务判成"非终态"**，于是一个已经 `phase=done`
    的项目被报成"会开烧"。第一版就是这么写的，当场误报了一次。
  · 咬 ②：**没停 + 有非终态任务** ⇒ `will_burn=True`、退出码 1（这才是要拦的形状）。
  · 咬 ③：**停了**（人叫停 / 有失败任务）⇒ 非终态任务再多也是安全的
    —— 判据是 `project.halt_state`，不是"有没有非终态任务"。
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import projects_state as ps                                          # noqa: E402
from singularity.scheduler.tracker import TaskStatus                 # noqa: E402


def _task(status):
    return SimpleNamespace(status=status)


def _proj(pid, phase="executing", task_ids=(), halted=False, failed=()):
    p = SimpleNamespace(
        id=pid, name=f"项目{pid}", phase=SimpleNamespace(value=phase),
        task_ids=list(task_ids), halted_reason="user_stop" if halted else "",
        halted_at=1.0 if halted else 0.0,
    )
    return p


class TestProjectsState(unittest.TestCase):
    def setUp(self):
        self._real_list = ps.proj_mod.list_all
        self._real_halt = ps.proj_mod.halt_state
        self._real_read = ps.tracker.read_task
        self.projs = []
        self.tasks = {}
        ps.proj_mod.list_all = lambda: self.projs
        ps.tracker.read_task = lambda tid: self.tasks.get(tid)
        # halt_state 是**生产的那个判据**，这里只喂它假数据 —— 不重写一份
        ps.proj_mod.halt_state = self._real_halt

    def tearDown(self):
        ps.proj_mod.list_all = self._real_list
        ps.proj_mod.halt_state = self._real_halt
        ps.tracker.read_task = self._real_read

    def test_终态任务不许被算成非终态(self):
        """咬 ①：`TaskStatus.DONE` 是个枚举，`str()` 出来是 `'TaskStatus.DONE'`。"""
        self.tasks["t1"] = _task(TaskStatus.DONE)
        self.tasks["t2"] = _task(TaskStatus.FAILED)
        self.projs = [_proj("p1", phase="done", task_ids=("t1", "t2"))]
        rows = ps.snapshot()
        self.assertEqual(rows[0]["non_terminal"], [],
                         f"终态任务被算成非终态了（枚举没走 .value?）：{rows[0]}")
        self.assertFalse(rows[0]["will_burn"], "phase=done 且没活干，却报成会开烧")

    def test_没停且有活干才是会开烧(self):
        """咬 ②：这才是要拦的形状。"""
        self.tasks["t1"] = _task(TaskStatus.PENDING)
        self.projs = [_proj("p1", task_ids=("t1",))]
        rows = ps.snapshot()
        self.assertTrue(rows[0]["will_burn"], rows[0])
        with contextlib.redirect_stdout(io.StringIO()):   # 别把报表混进测试输出
            self.assertEqual(ps.main([]), 1, "会开烧却没报 1")

    def test_停了就安全_哪怕任务还挂着(self):
        """咬 ③：判据是 `halt_state`，不是"有没有非终态任务"。

        有失败任务 ⇒ 派生 halted ⇒ `_advance_project` 早退 ⇒ 起后端不会推它。
        """
        self.tasks["t1"] = _task(TaskStatus.PENDING)
        self.tasks["t2"] = _task(TaskStatus.FAILED)
        self.projs = [_proj("p1", task_ids=("t1", "t2"))]      # 有 FAILED ⇒ 派生"停"
        rows = ps.snapshot()
        self.assertTrue(rows[0]["halted"] and rows[0]["halt_reason"] == "task_failed", rows[0])
        self.assertFalse(rows[0]["will_burn"], rows[0])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ps.main([]), 0)

    def test_任务文件读不到算非终态(self):
        """读不到任务文件 = 僵尸的形状，不能当"没事"放过去。"""
        self.projs = [_proj("p1", task_ids=("没了",))]
        rows = ps.snapshot()
        self.assertEqual(rows[0]["non_terminal"], ["没了"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
