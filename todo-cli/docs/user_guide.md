# User Guide

## Getting Started

After installing the todo CLI tool, you can start managing your tasks right away. The first time you use the tool, it will automatically create the necessary directories and files to store your todos.

## Commands

### Adding a Todo

To add a new todo, use the `add` command followed by the title of your task:

```bash
todo add "Buy groceries"
```

This will create a new todo with the specified title and assign it a unique ID.

### Listing Todos

To view all your todos, use the `list` command:

```bash
todo list
```

This will display all todos with their completion status and IDs. Completed todos will show `[x]` while pending todos will show `[ ]`.

### Completing a Todo

To mark a todo as completed, use the `complete` command followed by the ID of the todo:

```bash
todo complete 1
```

This will update the status of the specified todo to completed.

### Removing a Todo

To remove a todo, use the `remove` command followed by the ID of the todo:

```bash
todo remove 1
```

This will permanently delete the specified todo.

## Data Persistence

Todos are automatically saved to disk after each operation. The data is stored in JSON format in your system's standard data directory under the `todo-cli` folder.