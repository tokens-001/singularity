"""CLI layer - user interface for TODO tool."""
import argparse
import sys
from typing import List
from storage import Storage
from manager import TodoManager


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser for CLI."""
    parser = argparse.ArgumentParser(description='TODO Manager CLI')
    parser.add_argument('--file', default='todos.json', help='Data file path (default: todos.json)')
    
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Add command
    add_parser = subparsers.add_parser('add', help='Add a new TODO item')
    add_parser.add_argument('title', help='TODO item title')
    
    # List command
    subparsers.add_parser('list', help='List all TODO items')
    
    # Update command
    update_parser = subparsers.add_parser('update', help='Update a TODO item')
    update_parser.add_argument('id', type=int, help='Item ID')
    update_parser.add_argument('--title', help='New title')
    update_parser.add_argument('--completed', type=lambda x: x.lower() == 'true', help='Completion status (true/false)')
    
    # Delete command
    delete_parser = subparsers.add_parser('delete', help='Delete a TODO item')
    delete_parser.add_argument('id', type=int, help='Item ID')
    
    # Toggle command
    toggle_parser = subparsers.add_parser('toggle', help='Toggle completion status')
    toggle_parser.add_argument('id', type=int, help='Item ID')
    
    return parser


def format_item(item: dict) -> str:
    """Format a TODO item for display."""
    status = '✓' if item.get('completed') else '○'
    return f"[{item['id']:3d}] {status} {item['title']}"


def main() -> int:
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 0
    
    storage = Storage(args.file)
    manager = TodoManager(storage)
    
    if args.command == 'add':
        item = manager.add(args.title)
        print(f"Added: {format_item(item)}")
    
    elif args.command == 'list':
        items = manager.list_all()
        if not items:
            print("No TODO items")
        else:
            for item in items:
                print(format_item(item))
    
    elif args.command == 'update':
        item = manager.update(args.id, title=args.title, completed=args.completed)
        if item:
            print(f"Updated: {format_item(item)}")
        else:
            print(f"Item {args.id} not found", file=sys.stderr)
            return 1
    
    elif args.command == 'delete':
        if manager.delete(args.id):
            print(f"Deleted item {args.id}")
        else:
            print(f"Item {args.id} not found", file=sys.stderr)
            return 1
    
    elif args.command == 'toggle':
        item = manager.toggle(args.id)
        if item:
            print(f"Toggled: {format_item(item)}")
        else:
            print(f"Item {args.id} not found", file=sys.stderr)
            return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
