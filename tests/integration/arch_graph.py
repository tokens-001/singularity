"""从 import 关系生成架构图 —— 边来自 ast 解析，不是手写。

和 `docs/现状结构图.md` 那类手绘诊断图的区别：**这张图的每条边都能追溯到
"某文件里真的 import 了某模块"**。分组规则（下面 GROUPS）仍是我的判断，
所以它可信的是"耦合关系"，不是"分组合理"。

用法:
  .venv/bin/python tests/integration/arch_graph.py            # 打印邻接矩阵 + 边权重
  .venv/bin/python tests/integration/arch_graph.py --json out.json   # 出 archify 输入
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "singularity"

# 分组规则：按文件名前缀归并。**这是我的判断**，不是代码里定义的概念边界。
GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("api",     "API / Web 入口",   ("_api_admin", "_api_memory", "_api_monitor",
                                     "_api_projects", "_api_tasks", "_api")),
    ("cli",     "CLI",              ("__main__", "_cli_memory", "_cli_projects", "_cli_tasks")),
    ("sched",   "调度核心",          ("orchestrator", "tracker", "router", "dispatcher",
                                     "_dispatch_crud", "_dispatch_exec", "_dispatch_skills",
                                     "route_learner", "goal_loop", "permission")),
    ("exec",    "执行",              ("_exec", "_exec_context", "_task_runner")),
    ("flow",    "工作流 / 项目",     ("workflow", "_workflow_phases", "project",
                                     "chancellor", "task_templates", "_planner")),
    ("judge",   "委员会 / 融合",     ("execution_judge",)),
    ("gate",    "审查 / 门禁",       ("validator", "supervisor", "_review")),
    ("vcs",     "Worktree / 合并",   ("merge", "_worktree", "_git_worktree", "snapshot")),
    ("mem",     "记忆",              ("memory", "pre_search", "_memory_consolidator",
                                     "_memory_core", "_memory_experience", "_memory_graph",
                                     "_memory_lifecycle")),
    ("obs",     "观察者",            ("observer_agent", "_observer_answer", "_observer_client",
                                     "_observer_definition", "_observer_shared", "_observer_tools",
                                     "_observer_worker")),
    ("infra",   "模型 / 基础设施",   ("model_registry", "api_store", "config", "_io", "log",
                                     "witness", "roles", "_types", "_hooks", "_cache",
                                     "_model_breaker", "_token_budget", "_profiler",
                                     "bridge", "mcp", "codegraph", "neijinglu")),
]


def _group_of(stem: str, rel: Path) -> str:
    # 注意用 `in parts` 而不是 `parts[0] ==` —— 路径是相对 SRC 的，
    # executors/ 的真身是 `scheduler/executors/...`，第一段是 scheduler。
    # 早期写成 parts[0] 判断，导致 6 个 executor 文件全被兜底吞进 infra、exec 组少一半。
    if "executors" in rel.parts:
        return "exec"
    if "skills" in rel.parts:
        return "skills"
    for gid, _label, stems in GROUPS:
        if stem in stems:
            return gid
    return "infra"          # 兜底：没列到的归基础设施


def _targets(node: ast.ImportFrom, cur: Path) -> list[str]:
    """把 import 语句解析成它**真正依赖到**的模块点分路径（可多个）。

    两个坑，都踩过：
    ① `from singularity.scheduler import witness`（本仓库到处是，用来破循环依赖）
       里 `singularity.scheduler` 是**包目录**不是模块文件。按模块名解析会落到包的
       `__init__.py` 上，而 `__init__` 不属任何分组 → 兜底成 infra →
       **整个 scheduler 包被算成基础设施**（报 88 次，实际 29 次）。
    ② `from pkg import a, b` 是**两条**依赖（a 一个组、b 可能另一个组）。
       按语句计数会漏。

    ③ `from pkg.mod import sym` 的正解是 **pkg.mod**（sym 是函数/类，不是模块）。
       只按符号找会全部落空 —— 实测丢了 **453 次**（vs 算进来 36 条），
       图看起来干净，其实是假象。
    所以：**模块本身 + 各符号都作为候选，能落到真实 .py 文件的全部计入**。
    """
    if node.level:                                   # 相对导入 from . / from ..
        base = cur.parents[node.level - 1].relative_to(SRC)
        parts = list(base.parts)
        if node.module:
            parts += node.module.split(".")
    elif node.module and node.module.startswith("singularity"):
        parts = node.module.split(".")[1:]
    else:
        return []                                    # 外部包（httpx/flask…）不算内部耦合
    if not parts:
        return []
    base_dotted = "singularity." + ".".join(parts)
    return [base_dotted] + [f"{base_dotted}.{a.name}" for a in node.names]


def scan() -> tuple[dict[str, int], dict[tuple[str, str], int], int]:
    """返回 (组→文件数, (from组,to组)→import 次数, 组→跨组出边数)。"""
    files: dict[Path, str] = {}
    for py in SRC.rglob("*.py"):
        rel = py.relative_to(SRC)
        if "node_modules" in rel.parts:
            continue
        files[py] = _group_of(py.stem, rel)

    sizes: dict[str, int] = {}
    for g in files.values():
        sizes[g] = sizes.get(g, 0) + 1

    edges: dict[tuple[str, str], int] = {}
    dropped = 0
    for py, g_from in files.items():
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            # 每个符号各自解析；**只认真实模块文件**（不认包的 __init__ —— 认了就会
            # 把 "from pkg import x" 全算到包的兜底分组上）。解不出就不记这条边，
            # 但要计数：静默漏边会让图看起来"更干净"，那是假象。
            cands = _targets(node, py)
            if not cands:
                continue          # 标准库/第三方（json/os/httpx…）—— 不是内部耦合，不算丢弃
            hit = False
            for dotted in cands:
                parts = dotted.split(".")[1:]              # 去掉 singularity
                target = SRC.joinpath(*parts).with_suffix(".py")
                if target in files:
                    hit = True
                    if files[target] != g_from:
                        edges[(g_from, files[target])] = edges.get((g_from, files[target]), 0) + 1
            if not hit:
                dropped += 1       # 是对 singularity 的 import 但一个候选都没落到文件
    return sizes, edges, dropped


def main() -> int:
    sizes, edges, dropped = scan()
    labels = {g: lbl for g, lbl, _ in GROUPS}
    labels.update({"skills": "外部技能", "infra": labels.get("infra", "基础设施")})

    print(f"模块组 {len(sizes)} 个 / 跨组依赖 {len(edges)} 条"
          f" / 解析不出丢弃的 import {dropped} 次\n")
    print(f"{'组':<8}{'文件':<6}{'出边':<6}{'入边':<6}名称")
    outdeg: dict[str, int] = {}
    indeg: dict[str, int] = {}
    for (a, b), n in edges.items():
        outdeg[a] = outdeg.get(a, 0) + n
        indeg[b] = indeg.get(b, 0) + n
    for g in sorted(sizes, key=lambda x: -sizes[x]):
        print(f"{g:<8}{sizes[g]:<6}{outdeg.get(g,0):<6}{indeg.get(g,0):<6}{labels.get(g,'?')}")

    print("\n跨组依赖（import 次数，≥3 的才算主要耦合）:")
    for (a, b), n in sorted(edges.items(), key=lambda kv: -kv[1]):
        if n >= 3:
            print(f"  {labels.get(a,a):<18} → {labels.get(b,b):<18} {n}")

    if "--json" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--json") + 1])
        out.write_text(json.dumps({
            "sizes": sizes, "edges": {f"{a}->{b}": n for (a, b), n in edges.items()},
            "labels": labels,
        }, ensure_ascii=False, indent=2))
        print(f"\n已写出 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
