# Architecture

## Overview

The todo CLI application follows a modular architecture with clear separation of concerns between different components. The application is divided into several modules that handle specific responsibilities.

## Modules

### Main Module (`main.rs`)

The main module serves as the entry point for the application. It determines the location of the data file based on the system's standard data directory and initializes the CLI.

### CLI Module (`cli.rs`)

The CLI module handles user input and command parsing. It uses the `clap` crate to define and parse command-line arguments. This module maps user commands to appropriate actions in the application.

#### Key Responsibilities:
- Parse command-line arguments
- Define available subcommands (add, list, complete, remove)
- Route commands to appropriate handlers
- Handle user interactions

### Storage Module (`storage.rs`)

The storage module manages persistence of todos to disk. It handles reading from and writing to the JSON data file, including error handling and recovery mechanisms.

#### Key Responsibilities:
- Load todos from the data file
- Save todos to the data file
- Handle file I/O errors
- Implement recovery mechanisms for corrupted data files

### Todo Module (`todo.rs`)

The todo module defines the data structure for a todo item and provides validation logic. It includes methods for converting between the internal representation and JSON format.

#### Key Responsibilities:
- Define the Todo struct
- Provide validation for todo properties
- Handle serialization/deserialization to/from JSON
- Manage todo lifecycle operations

### Library Module (`lib.rs`)

The library module exports the public API of the application, making it accessible to both the binary target and potential external consumers.

## Data Flow

1. User runs a command via the CLI
2. The CLI module parses the command and routes it to the appropriate handler
3. The handler interacts with the storage module to load/save todos
4. The storage module reads/writes the JSON data file
5. Results are displayed back to the user

## Error Handling

The application implements robust error handling throughout all modules:
- Input validation prevents invalid todo entries
- File I/O errors trigger recovery mechanisms
- Graceful degradation when data files are corrupted
- User-friendly error messages

## Testing Strategy

The application includes both unit tests within each module and integration tests to verify the interaction between modules.