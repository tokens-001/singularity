# Todo CLI

A simple command-line interface todo application written in Rust.

## Features

- Add new tasks
- List all tasks
- Mark tasks as completed
- Remove tasks

## Installation

First, make sure you have Rust and Cargo installed on your system. Then:

```bash
cd todo-cli
cargo build --release
```

The executable will be available at `target/release/todo`.

## Usage

```bash
# Add a new task
todo add "Buy groceries"

# List all tasks
todo list

# Mark a task as completed (replace ID with actual task ID)
todo complete 1

# Remove a task (replace ID with actual task ID)
todo remove 1
```

## Data Storage

Tasks are stored in JSON format in your system's data directory under `todo-cli/tasks.json`.
On Linux/Mac, this is typically `~/.local/share/todo-cli/tasks.json`
On Windows, this is typically `%APPDATA%\todo-cli\tasks.json`

## Dependencies

- `clap` - For command-line argument parsing
- `serde` - For serialization/deserialization
- `serde_json` - For JSON handling
- `dirs` - For cross-platform directory management