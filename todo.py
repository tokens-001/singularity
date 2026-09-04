#!/usr/bin/env python3
"""命令行待办清单工具：支持添加、查看、完成、删除。

用法示例：
    python3 todo.py add "买牛奶"
    python3 todo.py add 写周报 --priority high
    python3 todo.py list
    python3 todo.py list --all
    python3 todo.py done 1
    python3 todo.py delete 2

数据默认保存在 ~/.todo_cli/todos.json，可用环境变量 TODO_FILE 覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_STORE = Path.home() / ".todo_cli" / "todos.json"
PRIORITIES = {"low", "medium", "high"}


@dataclass
class Task:
    id: int
    title: str
    done: bool = False
    priority: str = "medium"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "done": self.done,
            "priority": self.priority,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        return cls(
            id=int(data["id"]),
            title=str(data["title"]),
            done=bool(data.get("done", False)),
            priority=str(data.get("priority", "medium")),
            created_at=str(data.get("created_at", "")),
        )


def store_path() -> Path:
    return Path(os.environ.get("TODO_FILE", DEFAULT_STORE))


def load_tasks(path: Path) -> list[Task]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        tasks = [Task.from_dict(item) for item in raw]
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        print(f"警告：数据文件损坏，已忽略：{path}", file=sys.stderr)
        return []
    return sorted(tasks, key=lambda t: t.id)


def save_tasks(path: Path, tasks: list[Task]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [task.to_dict() for task in sorted(tasks, key=lambda t: t.id)]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def next_id(tasks: list[Task]) -> int:
    return max((task.id for task in tasks), default=0) + 1


def cmd_add(args: argparse.Namespace) -> int:
    path = store_path()
    tasks = load_tasks(path)
    task = Task(id=next_id(tasks), title=" ".join(args.title), priority=args.priority)
    tasks.append(task)
    save_tasks(path, tasks)
    print(f"已添加 #{task.id}：{task.title}（优先级 {task.priority}）")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    tasks = load_tasks(store_path())
    if not tasks:
        print("暂无待办事项。")
        return 0
    visible = tasks if args.all else [t for t in tasks if not t.done]
    if not visible:
        print("没有未完成的待办，使用 --all 查看全部。")
        return 0
    for task in visible:
        mark = "✔" if task.done else " "
        print(f"[{mark}] #{task.id:<3} ({task.priority:<6}) {task.title}")
    pending = sum(1 for t in tasks if not t.done)
    print(f"\n共 {len(tasks)} 项，未完成 {pending} 项。")
    return 0


def _find(tasks: list[Task], task_id: int) -> Task | None:
    for task in tasks:
        if task.id == task_id:
            return task
    return None


def cmd_done(args: argparse.Namespace) -> int:
    path = store_path()
    tasks = load_tasks(path)
    task = _find(tasks, args.id)
    if task is None:
        print(f"未找到编号 #{args.id} 的任务。", file=sys.stderr)
        return 1
    task.done = True
    save_tasks(path, tasks)
    print(f"已完成 #{task.id}：{task.title}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    path = store_path()
    tasks = load_tasks(path)
    task = _find(tasks, args.id)
    if task is None:
        print(f"未找到编号 #{args.id} 的任务。", file=sys.stderr)
        return 1
    tasks = [t for t in tasks if t.id != task.id]
    save_tasks(path, tasks)
    print(f"已删除 #{task.id}：{task.title}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="命令行待办清单工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="添加任务")
    p_add.add_argument("title", nargs="+", help="任务内容")
    p_add.add_argument("--priority", choices=sorted(PRIORITIES), default="medium", help="优先级")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="查看任务")
    p_list.add_argument("--all", action="store_true", help="同时显示已完成任务")
    p_list.set_defaults(func=cmd_list)

    p_done = sub.add_parser("done", help="完成任务")
    p_done.add_argument("id", type=int, help="任务编号")
    p_done.set_defaults(func=cmd_done)

    p_delete = sub.add_parser("delete", help="删除任务")
    p_delete.add_argument("id", type=int, help="任务编号")
    p_delete.set_defaults(func=cmd_delete)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
