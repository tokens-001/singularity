# Todo CLI

A simple command-line interface for managing your tasks.

## Features

- Add new tasks
- List all tasks
- Mark tasks as completed
- Remove tasks

## Installation

```bash
cargo install --path .
```

## Usage

```bash
# Add a new task
todo add "Buy groceries"

# List all tasks
todo list

# Mark a task as completed (replace ID with actual task ID)
todo complete 3

# Remove a task (replace ID with actual task ID)
todo remove 5
```

## Commands

- `add <title>` - Add a new task with the given title
- `list` - List all tasks with their completion status
- `complete <id>` - Mark a task as completed
- `remove <id>` - Remove a task

## Data Storage

Tasks are stored in JSON format in your system's data directory under `todo-cli/tasks.json`.

## License

MIT