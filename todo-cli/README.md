# Todo CLI

A simple command-line interface tool for managing your todos.

## Features

- Add new tasks
- List all tasks
- Mark tasks as completed
- Delete tasks

## Installation

To install this tool, you need to have Rust and Cargo installed on your system. Then run:

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
todo complete 1

# Delete a task (replace ID with actual task ID)
todo delete 1
```

## Data Storage

The application stores your tasks in a JSON file located at `~/.local/share/todo-cli/tasks.json` on Linux/macOS or `%APPDATA%\todo-cli\tasks.json` on Windows.