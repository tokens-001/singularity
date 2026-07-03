"""__main__.py — CLI 入口

子命令:
  scheduler add "任务"     入队即返回 (不等执行)
  scheduler run "任务"     入队 + 立即跑 (等价于 add + loop 一轮)
  scheduler loop           常驻循环: 取队→执行→取队→执行... (Ctrl+C 停)
  scheduler rollback ID    手动回滚到某 snapshot (审计 4.4)
  scheduler apply ID       手动 apply E+/Planner patch (审计 6.5)
  scheduler status         查看队列状态 + 各层负载
"""

from __future__ import annotations
import json
import sys
import time
import signal

from singularity.scheduler import config
from singularity.scheduler import dispatcher as disp_mod
from singularity.scheduler import snapshot as snap_mod
from singularity.scheduler import orchestrator
from singularity.scheduler import tracker
from singularity.scheduler.tracker import TaskStatus
def _cmd_project_delete(project_id: str) -> int:
    from .project import load as load_proj
    proj = load_proj(project_id)
    if proj is None:
        print(f"项目不存在: {project_id}", file=sys.stderr)
        return 1
    # 删除关联的任务文件
    from . import tracker as _tk
    for tid in proj.task_ids:
        p = _tk._path(tid)
        if p.exists():
            p.unlink()
    # 删除项目文件
    from .project import _path as _proj_path
    _proj_path(project_id).unlink()
    print(f"[project] 已删除: {proj.id[:8]} {proj.name}")
    return 0

from singularity.scheduler.project import Phase

_LOOP_POLL_SECS = 3  # 队列空时的轮询间隔


def main(argv: list) -> int:
    if not argv:
        print(
            "用法: python3 -m scheduler add|run|loop|rollback|apply|status|merge|memory [参数]",
            file=sys.stderr,
        )
        return 2

    cmd = argv[0]

    if cmd == "add" and len(argv) >= 2:
        return _cmd_add(" ".join(argv[1:]))
    if cmd == "run" and len(argv) >= 2:
        # 修复 #4: run 支持 --concurrent N
        rest, concurrent = _parse_concurrent(argv[1:])
        return _cmd_run(" ".join(rest), max_concurrent=concurrent)
    if cmd == "loop":
        # 修复 #4: loop 支持 --concurrent N
        rest, concurrent = _parse_concurrent(argv[1:])
        return _cmd_loop(max_concurrent=concurrent)
    if cmd == "rollback" and len(argv) >= 2:
        return _cmd_rollback(argv[1])
    if cmd == "apply" and len(argv) >= 2:
        return _cmd_apply(argv[1])
    if cmd == "status":
        return _cmd_status()
    if cmd == "merge" and len(argv) >= 2:
        # 修复 #10: merge list / merge resolve <id> --manual|--abort
        return _cmd_merge(argv[1:])
    if cmd == "memory":
        return _cmd_memory(argv[1:])
    if cmd == "project":
        return _cmd_project(argv[1:])

    # 兼容旧用法: 直接跟任务文本
    rest, concurrent = _parse_concurrent(argv)
    task = " ".join(rest)
    return _cmd_run(task, max_concurrent=concurrent)


def _parse_concurrent(args: list) -> tuple:
    """从 argv 提取 --concurrent N (修复 #4)。返回 (剩余args, concurrent)。"""
    rest = []
    concurrent = 1
    i = 0
    while i < len(args):
        if args[i] == "--concurrent" and i + 1 < len(args):
            try:
                concurrent = int(args[i + 1])
                i += 2
                continue
            except ValueError:
                pass
        rest.append(args[i])
        i += 1
    return rest, concurrent



from singularity.scheduler._cli_tasks import *  # noqa: F401,F403
from singularity.scheduler._cli_memory import *  # noqa: F401,F403
from singularity.scheduler._cli_projects import *  # noqa: F401,F403
