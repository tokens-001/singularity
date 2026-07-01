use std::path::PathBuf;

use clap::{Parser, Subcommand};

use crate::storage::Storage;
use crate::todo::Todo;

#[derive(Parser)]
#[command(name = "todo")]
#[command(version = "0.1.0")]
#[command(about = "A simple CLI todo application", long_about = None)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Commands,
}

#[derive(Subcommand)]
pub enum Commands {
    /// Add a new task
    Add {
        /// Task title
        #[arg(required = true)]
        title: String,
    },
    /// List all tasks
    List,
    /// Mark a task as completed
    Complete {
        /// Task ID
        id: usize,
    },
    /// Remove a task
    Remove {
        /// Task ID
        id: usize,
    },
}

pub fn run(data_file: PathBuf) {
    let cli = Cli::parse();
    let storage = Storage::new(data_file);

    match cli.command {
        Commands::Add { title } => add_todo(&title, &storage),
        Commands::List => list_todos(&storage),
        Commands::Complete { id } => complete_todo(id, &storage),
        Commands::Remove { id } => remove_todo(id, &storage),
    }
}

fn add_todo(title: &str, storage: &Storage) {
    let todo = Todo::new(title);
    if let Err(err) = todo.validate() {
        eprintln!("Error: {}", err);
        std::process::exit(1);
    }

    let mut todos = storage.load();
    let new_id = todos.iter().map(|t| t.id.unwrap_or(0)).max().unwrap_or(0) + 1;
    todos.push(todo.with_id(new_id));

    if let Err(err) = storage.save(&todos) {
        eprintln!("Error: could not save todo: {}", err);
        std::process::exit(1);
    }

    println!("Added todo #{}: {}", new_id, title);
}

fn list_todos(storage: &Storage) {
    let todos = storage.load();
    if todos.is_empty() {
        println!("No todos found.");
        return;
    }

    for todo in todos {
        let status = if todo.completed { "[x]" } else { "[ ]" };
        println!("{} {}: {}", status, todo.id.unwrap_or(0), todo.title);
    }
}

fn complete_todo(id: usize, storage: &Storage) {
    let mut todos = storage.load();
    if let Some(todo) = todos.iter_mut().find(|t| t.id == Some(id)) {
        todo.completed = true;
        if let Err(err) = storage.save(&todos) {
            eprintln!("Error: could not save todo: {}", err);
            std::process::exit(1);
        }
        println!("Completed todo #{}", id);
    } else {
        println!("Todo #{} not found.", id);
    }
}

fn remove_todo(id: usize, storage: &Storage) {
    let mut todos = storage.load();
    let initial_len = todos.len();
    todos.retain(|t| t.id != Some(id));

    if todos.len() == initial_len {
        println!("Todo #{} not found.", id);
    } else if let Err(err) = storage.save(&todos) {
        eprintln!("Error: could not save todo: {}", err);
        std::process::exit(1);
    } else {
        println!("Removed todo #{}", id);
    }
}
