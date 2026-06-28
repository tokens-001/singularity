#!/usr/bin/env python3
"""导入完整性检查 — 验证所有模块无 ImportError / NameError。

跑法: QIDIAN_SKIP_EMBED=1 python3 tests/test_imports.py
绿=导入链完整, 红=有模块无法导入。

覆盖: scheduler 52 + observer 3 + skills 1 + web 2 + 根脚本 2 = 60 模块
"""

import os
import subprocess
import sys
from pathlib import Path

os.environ["QIDIAN_SKIP_EMBED"] = "1"
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "src"))

PASS = FAIL = 0

# 可安全 import 的模块（无 import 时副作用）
SAFE_IMPORTS = [
    # scheduler
    "singularity.scheduler.config",
    "singularity.scheduler.witness",
    "singularity.scheduler.log",
    "singularity.scheduler._types",
    "singularity.scheduler._auth",
    "singularity.scheduler._cache",
    "singularity.scheduler._io",
    "singularity.scheduler._profiler",
    "singularity.scheduler._token_budget",
    "singularity.scheduler._planner",
    "singularity.scheduler._review",
    "singularity.scheduler._exec_context",
    "singularity.scheduler._git_worktree",
    "singularity.scheduler._worktree",
    "singularity.scheduler._task_runner",
    "singularity.scheduler.api_store",
    "singularity.scheduler.bridge",
    "singularity.scheduler.chancellor",
    "singularity.scheduler.codegraph",
    "singularity.scheduler.dispatcher",
    "singularity.scheduler.execution_judge",
    "singularity.scheduler.goal_loop",
    "singularity.scheduler.handoff",
    "singularity.scheduler.mcp",
    "singularity.scheduler.merge",
    "singularity.scheduler.model_profile",
    "singularity.scheduler.model_registry",
    "singularity.scheduler.orchestrator",
    "singularity.scheduler.permission",
    "singularity.scheduler.pre_search",
    "singularity.scheduler.project",
    "singularity.scheduler.roles",
    "singularity.scheduler.route_learner",
    "singularity.scheduler.router",
    "singularity.scheduler.snapshot",
    "singularity.scheduler.supervisor",
    "singularity.scheduler.task_templates",
    "singularity.scheduler.tracker",
    "singularity.scheduler.validator",
    "singularity.scheduler.workflow",
    "singularity.scheduler.memory",
    # observer
    "singularity.observer.config",
    "singularity.observer.state_sampler",
    # skills
    "singularity.skills.skill_loader",
    # web
    "singularity.web.app",
    # executors
    "singularity.scheduler.executors.base",
]

# 需子进程隔离的模块（import 时有副作用如 asyncio.run / Flask 启动）
ISOLATED_IMPORTS = [
    "singularity.scheduler.__main__",
    "singularity.scheduler.neijinglu",
    "singularity.scheduler.observer_agent",
    "singularity.scheduler._exec",
    "singularity.scheduler.executors.anthropic_api",
    "singularity.scheduler.executors.claude_cli",
    "singularity.scheduler.executors.openai_agent",
    "singularity.scheduler.executors.zhipu_api",
    "singularity.observer.server",
]


def check_safe(module: str) -> bool:
    """直接 import，返回是否成功。"""
    try:
        __import__(module)
        return True
    except Exception as e:
        print(f"  ❌ {module}: {e}")
        return False


def check_isolated(module: str) -> bool:
    """子进程 import，避免副作用污染测试进程。"""
    code = f"import sys; sys.path.insert(0, '{PROJECT / 'src'}'); "
    code += f"import os; os.environ['QIDIAN_SKIP_EMBED']='1'; "
    code += f"__import__('{module}'); print('OK')"
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30,
        cwd=str(PROJECT),
    )
    if r.returncode != 0:
        err = (r.stderr or "").splitlines()
        last = err[-1] if err else "unknown"
        print(f"  ❌ {module}: {last[:100]}")
        return False
    return True


def check_shell(path: str) -> bool:
    """bash -n 语法检查。"""
    r = subprocess.run(
        ["bash", "-n", str(PROJECT / path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"  ❌ {path}: {r.stderr.strip()[:100]}")
        return False
    return True


def main() -> None:
    global PASS, FAIL
    PASS = FAIL = 0

    print("── 安全 import (直接) ──")
    for mod in SAFE_IMPORTS:
        if check_safe(mod):
            PASS += 1
        else:
            FAIL += 1

    print(f"\n── 隔离 import (子进程) ──")
    for mod in ISOLATED_IMPORTS:
        if check_isolated(mod):
            PASS += 1
        else:
            FAIL += 1

    print(f"\n── Shell 脚本语法 ──")
    for sh in ["run.sh", "start.sh"]:
        if check_shell(sh):
            PASS += 1
        else:
            FAIL += 1

    print(f"\n{'='*40}")
    total = PASS + FAIL
    print(f"{'✅ 全通过' if FAIL == 0 else '❌ 有失败'}  通过 {PASS}/{total}" if total else "无测试")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
