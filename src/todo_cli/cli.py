"""
CLI layer — user-facing commands.

This layer knows about user input and output formatting but does NOT
directly operate files. All persistence goes through TodoService.
"""

import argparse
import json
import logging
import sys
from typing import Sequence

from .service import TodoService
from .storage import Storage

DEFAULT_DATA_FILE = "todos.json"


def _get_service(data_file: str) -> TodoService:
    storage = Storage(data_file)
    service = TodoService(storage)
    service.load()
    return service


def cmd_add(args: argparse.Namespace) -> None:
    service = _get_service(args.data_file)
    todo = service.add(args.title, args.description)
    print(f"Added: [{todo['id']}] {todo['title']}")


def cmd_delete(args: argparse.Namespace) -> None:
    service = _get_service(args.data_file)
    if service.delete(args.id):
        print(f"Deleted: {args.id}")
    else:
        print(f"Not found: {args.id}", file=sys.stderr)
        sys.exit(1)


def cmd_update(args: argparse.Namespace) -> None:
    service = _get_service(args.data_file)
    updates = {}
    if args.title:
        updates["title"] = args.title
    if args.description is not None:
        updates["description"] = args.description
    if args.status:
        updates["status"] = args.status
    if not updates:
        print("No fields to update", file=sys.stderr)
        sys.exit(1)
    todo = service.update(args.id, **updates)
    if todo:
        print(f"Updated: [{todo['id']}] {todo['title']}")
    else:
        print(f"Not found: {args.id}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> None:
    service = _get_service(args.data_file)
    todos = service.list_all(status=args.status)
    if not todos:
        print("No todos found.")
        return
    for todo in todos:
        status_icon = {"pending": "○", "done": "✓", "in_progress": "◐"}.get(
            todo["status"], "?"
        )
        print(f"  {status_icon} [{todo['id']}] {todo['title']} ({todo['status']})")
        if todo.get("description"):
            print(f"       {todo['description']}")
    print(f"\nTotal: {len(todos)}")


def cmd_show(args: argparse.Namespace) -> None:
    service = _get_service(args.data_file)
    todo = service.get(args.id)
    if todo is None:
        print(f"Not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(todo, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todo",
        description="A command-line TODO manager",
    )
    parser.add_argument(
        "--data-file",
        default=DEFAULT_DATA_FILE,
        help=f"Path to data file (default: {DEFAULT_DATA_FILE})",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # add
    p_add = subparsers.add_parser("add", help="Add a new todo")
    p_add.add_argument("title", help="Todo title")
    p_add.add_argument("-d", "--description", default="", help="Todo description")

    # delete
    p_del = subparsers.add_parser("delete", help="Delete a todo")
    p_del.add_argument("id", help="Todo ID to delete")

    # update
    p_upd = subparsers.add_parser("update", help="Update a todo")
    p_upd.add_argument("id", help="Todo ID to update")
    p_upd.add_argument("-t", "--title", help="New title")
    p_upd.add_argument("-d", "--description", help="New description")
    p_upd.add_argument(
        "-s", "--status",
        choices=["pending", "in_progress", "done"],
        help="New status",
    )

    # list
    p_list = subparsers.add_parser("list", help="List todos")
    p_list.add_argument(
        "-s", "--status",
        choices=["pending", "in_progress", "done"],
        help="Filter by status",
    )

    # show
    p_show = subparsers.add_parser("show", help="Show todo details")
    p_show.add_argument("id", help="Todo ID")

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    commands = {
        "add": cmd_add,
        "delete": cmd_delete,
        "update": cmd_update,
        "list": cmd_list,
        "show": cmd_show,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
