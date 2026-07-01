# Todo CLI

A simple command-line interface for managing todos.

## Features

- Add new todos
- List all todos
- Mark todos as completed
- Remove todos

## Installation

To build and install the todo CLI tool, you need Rust installed on your system. You can install Rust using [rustup](https://rustup.rs/).

```bash
cargo build --release
```

The executable will be available at `target/release/todo-cli`.

## Usage

```bash
# Add a new todo
todo add "Buy groceries"

# List all todos
todo list

# Mark a todo as completed (replace ID with actual todo ID)
todo complete 1

# Remove a todo (replace ID with actual todo ID)
todo remove 1
```

## Data Storage

Todos are stored in JSON format in your system's default data directory under `todo-cli/tasks.json`.
On Linux this is typically `~/.local/share/todo-cli/tasks.json`
On macOS this is typically `~/Library/Application Support/todo-cli/tasks.json`
On Windows this is typically `%APPDATA%\todo-cli\tasks.json`

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.