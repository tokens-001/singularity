# Todo CLI

A simple command-line interface for managing your todos.

## Features

- Add new todos
- List all todos with their completion status
- Mark todos as completed
- Remove todos
- Persistent storage using JSON files

## Installation

To build and install this CLI tool, you'll need Rust and Cargo installed. Then:

```bash
git clone <repository-url>
cd todo-cli
cargo build --release
```

The executable will be available at `target/release/todo-cli`.

## Usage

```bash
# Add a new todo
todo add "Buy groceries"

# List all todos
todo list

# Mark a todo as completed (replace ID with actual number)
todo complete 1

# Remove a todo (replace ID with actual number)
todo remove 1
```

Todos are stored in `~/.local/share/todo-cli/tasks.json` on Linux/macOS or `%APPDATA%\todo-cli\tasks.json` on Windows by default.