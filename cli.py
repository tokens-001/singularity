"""CLI 层：命令行交互入口，不直接操作文件。"""

import argparse
import sys
from pathlib import Path

from service import TodoService
from storage import Storage

DEFAULT_DATA_PATH = Path.home() / ".todo" / "data.json"


def get_service(data_path: Path = DEFAULT_DATA_PATH) -> TodoService:
    data_path.parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(data_path)
    return TodoService(storage)


def cmd_add(args: argparse.Namespace) -> None:
    svc = get_service()
    todo = svc.create_todo(title=args.title, description=args.description or "")
    print(f"Created todo {todo.id}: {todo.title}")


def cmd_list(args: argparse.Namespace) -> None:
    svc = get_service()
    todos = svc.list_todos()
    if not todos:
        print("No todos found.")
        return
    for t in todos:
        mark = "[x]" if t.done else "[ ]"
        print(f"{mark} {t.id} | {t.title}")


def cmd_update(args: argparse.Namespace) -> None:
    svc = get_service()
    changes: dict[str, object] = {}
    if args.title is not None:
        changes["title"] = args.title
    if args.description is not None:
        changes["description"] = args.description
    if args.done is not None:
        changes["done"] = args.done
    todo = svc.update_todo(args.id, **changes)
    print(f"Updated todo {todo.id}: {todo.title}")


def cmd_delete(args: argparse.Namespace) -> None:
    svc = get_service()
    if svc.delete_todo(args.id):
        print(f"Deleted todo {args.id}")
    else:
        print(f"Todo {args.id} not found")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="todo", description="Simple CLI TODO tool")
    sub = parser.add_subparsers(dest="command", required=True)

    add_parser = sub.add_parser("add", help="Add a new todo")
    add_parser.add_argument("title", help="Todo title")
    add_parser.add_argument("--description", "-d", default="", help="Description")
    add_parser.set_defaults(func=cmd_add)

    list_parser = sub.add_parser("list", help="List all todos")
    list_parser.set_defaults(func=cmd_list)

    upd_parser = sub.add_parser("update", help="Update a todo")
    upd_parser.add_argument("id", help="Todo ID")
    upd_parser.add_argument("--title", "-t", default=None, help="New title")
    upd_parser.add_argument("--description", "-d", default=None, help="New description")
    upd_parser.add_argument("--done", action="store_true", default=None, help="Mark as done")
    upd_parser.add_argument("--undone", action="store_false", dest="done", help="Mark as not done")
    upd_parser.set_defaults(func=cmd_update)

    del_parser = sub.add_parser("delete", help="Delete a todo")
    del_parser.add_argument("id", help="Todo ID")
    del_parser.set_defaults(func=cmd_delete)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
