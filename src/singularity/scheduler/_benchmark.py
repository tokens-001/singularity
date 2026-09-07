"""模型能力一键基准评测。

复用 OpenAIAgentExecutor 跑 3 个金标准编码任务（fib/todo/sql），
pytest 自动判定通过，按结果定 rating/speed/strengths/max_turns 写回模型库。

轻量基准，非权威——只测基本编码能力，不宣称 SWE-bench 级评级。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# 速度分档阈值（秒，3 任务平均 wall-clock）
FAST_UNDER = 90
SLOW_OVER = 240


@dataclass
class GoldenTask:
    key: str          # 任务标识（目录/文件名前缀）
    label: str        # 映射到 strengths 的中文标签
    prompt: str       # 需求 prompt（喂给模型）
    test_code: str    # 验收 pytest 代码，写为 test_<key>.py


GOLDEN_TASKS: list[GoldenTask] = [
    GoldenTask(
        key="fib",
        label="基础编码",
        prompt=(
            "在项目目录写一个 fibonacci.py，定义 fib(n:int)->int 返回第 n 个斐波那契数"
            "（fib(0)=0, fib(1)=1, fib(n)=fib(n-1)+fib(n-2)）。用 write_file 写入完整可运行代码。"
        ),
        test_code="""from fibonacci import fib

def test_fib():
    assert fib(0) == 0
    assert fib(1) == 1
    assert fib(2) == 1
    assert fib(10) == 55
    assert fib(20) == 6765
""",
    ),
    GoldenTask(
        key="todo",
        label="结构化编程",
        prompt=(
            "在项目目录写一个 todo.py，定义类 TodoList：add(task) 添加、remove(task) 删除、"
            "list() 返回未完成任务列表(保持插入顺序)、mark_done(task) 标记完成、done() 返回已完成任务列表。"
            "纯内存列表实现，无需持久化。用 write_file 写入完整可运行代码。"
        ),
        test_code="""from todo import TodoList

def test_todo_flow():
    t = TodoList()
    t.add('a'); t.add('b'); t.add('c')
    assert t.list() == ['a', 'b', 'c']
    t.mark_done('a')
    assert t.list() == ['b', 'c']
    assert t.done() == ['a']
    t.remove('b')
    assert t.list() == ['c']
    assert t.done() == ['a']
""",
    ),
    GoldenTask(
        key="sql",
        label="数据查询",
        prompt=(
            "在项目目录写一个 query.py，定义 top_earners(conn, n=3)：传入 sqlite3 连接，"
            "表 employees(id INTEGER, name TEXT, salary REAL)，返回 salary 最高的 n 个 "
            "(name, salary) 元组，按 salary 降序。只用标准库 sqlite3。用 write_file 写入完整可运行代码。"
        ),
        test_code="""import sqlite3
from query import top_earners

def test_top_earners():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE employees (id INTEGER, name TEXT, salary REAL)')
    conn.executemany('INSERT INTO employees VALUES (?,?,?)',
        [(1,'Alice',100),(2,'Bob',300),(3,'Cindy',200),(4,'Dan',250)])
    assert top_earners(conn, 3) == [('Bob',300),('Dan',250),('Cindy',200)]
""",
    ),
]


def _judge(task_dir: Path, task: GoldenTask) -> bool:
    """模型产出文件后，在同目录跑验收 pytest，returncode==0 判通过。"""
    (task_dir / f"test_{task.key}.py").write_text(task.test_code, encoding="utf-8")
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", f"test_{task.key}.py"],
            capture_output=True, text=True, cwd=str(task_dir), timeout=120,
        )
        return r.returncode == 0
    except Exception:
        return False


def _summarize(results: list[dict], m) -> dict:
    """评分定档。纯函数，可单测。m 是 ModelEntry（读 max_turns/rating 等原有值做回退）。"""
    n_pass = sum(1 for r in results if r["passed"])
    avg = sum(r["elapsed"] for r in results) / len(results) if results else 0.0
    rating = {3: "S", 2: "A+", 1: "A", 0: "?"}[n_pass]
    speed = "fast" if avg < FAST_UNDER else ("slow" if avg > SLOW_OVER else "medium")
    strengths = [r["label"] for r in results if r["passed"]]
    turns = [r["turns"] for r in results if r["turns"] > 0]
    max_turns = max(3, min(8, max(turns) + 2)) if turns else getattr(m, "max_turns", 5)
    return {
        "n_pass": n_pass, "rating": rating, "speed": speed,
        "strengths": strengths, "max_turns": max_turns,
    }


def _append_note(notes: str, s: dict) -> str:
    tag = f"轻量基准 {s['n_pass']}/{len(GOLDEN_TASKS)} 通过(非权威)"
    return f"{notes} | {tag}" if notes else tag


def run_benchmark(model_id: str) -> dict:
    """跑 3 个金标准任务，定档并写回模型库。返回结果 dict。"""
    from . import dispatcher, model_registry
    from .executors.openai_agent import OpenAIAgentExecutor

    m = model_registry.get(model_id)
    if not m:
        return {"ok": False, "error": "模型不存在"}

    cfg = dispatcher._build_agent_from_registry(model_id)
    if not cfg or not (cfg.get("entry") and cfg.get("api_key_env")):
        return {"ok": False, "error": "模型未配置 API（无 entry/api_key_env，先添加 key）"}
    cfg["max_turns"] = 8  # 基准本地给足轮次；写回的是实测 turns 定出的 max_turns

    tmp = Path(tempfile.mkdtemp(prefix="qidian-bench-"))
    try:
        results = []
        for task in GOLDEN_TASKS:
            task_dir = tmp / task.key
            task_dir.mkdir()
            ex = OpenAIAgentExecutor(cfg, task.prompt, f"bench-{task.key}", cwd=str(task_dir))
            r = ex.run()
            passed = _judge(task_dir, task)
            turns = sum(1 for e in r.tool_events if e.get("kind") == "tool:start")
            results.append({
                "key": task.key, "label": task.label, "passed": passed,
                "elapsed": r.elapsed, "turns": turns, "error": r.error,
            })
        s = _summarize(results, m)

        model_registry.add_model(
            model_id, m.provider, m.display, m.recommended_for,
            speed=s["speed"], cost=m.cost, rating=s["rating"],
            reasoning=m.reasoning, max_turns=s["max_turns"],
            notes=_append_note(m.notes, s), strengths=s["strengths"],
        )
        return {
            "ok": True, "model_id": model_id,
            "passed": s["n_pass"], "total": len(GOLDEN_TASKS),
            "rating": s["rating"], "speed": s["speed"],
            "strengths": s["strengths"], "max_turns": s["max_turns"],
            "tasks": results, "note": "轻量基准，非权威",
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
