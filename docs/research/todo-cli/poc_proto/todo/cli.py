"""PoC 原型：argparse 子命令（仅用于技术预研验证）。"""

from __future__ import annotations

import argparse
import json
import sys

from . import store


def _fmt_line(idx: int, item: dict) -> str:
    mark = "x" if item.get("done") else " "
    return f"[{mark}] {item.get('id')}. {item.get('text')}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="todo", description="命令行待办工具")
    p.add_argument("--file", default=None, help="数据文件路径（默认 ~/.todo.json）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="新增待办")
    a.add_argument("text", nargs="+")

    li = sub.add_parser("list", help="列出待办")
    li.add_argument("--all", action="store_true", help="包含已完成")

    d = sub.add_parser("done", help="标记完成")
    d.add_argument("id", type=int)

    r = sub.add_parser("remove", help="删除待办")
    r.add_argument("id", type=int)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "add":
            item = store.add(" ".join(args.text), path=args.file)
            print(json.dumps(item, ensure_ascii=False) if args.json else f"已添加 #{item['id']}: {item['text']}")
        elif args.cmd == "list":
            items = store.list(path=args.file, include_done=getattr(args, "all", False))
            if args.json:
                print(json.dumps(items, ensure_ascii=False))
            elif not items:
                print("（无待办）")
            else:
                for i, item in enumerate(items, 1):
                    print(_fmt_line(i, item))
        elif args.cmd == "done":
            item = store.done(args.id, path=args.file)
            if item is None:
                print(f"未找到 #{args.id}", file=sys.stderr)
                return 1
            print(json.dumps(item, ensure_ascii=False) if args.json else f"已完成 #{item['id']}: {item['text']}")
        elif args.cmd == "remove":
            if not store.remove(args.id, path=args.file):
                print(f"未找到 #{args.id}", file=sys.stderr)
                return 1
            print(f"已删除 #{args.id}")
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"文件错误: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
