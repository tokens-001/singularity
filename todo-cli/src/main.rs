use clap::{Parser, Subcommand};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;

#[derive(Parser)]
#[command(name = "todo")]
#[command(version = "0.1.0")]
#[command(about = "A simple CLI todo application", long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Add a new task
    Add {
        /// Task description
        #[arg(required = true)]
        description: String,
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

#[derive(Serialize, Deserialize)]
struct Task {
    id: usize,
    description: String,
    completed: bool,
}

fn main() {
    let cli = Cli::parse();
    
    // Get data directory
    let data_dir = dirs::data_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("todo-cli");
    
    if !data_dir.exists() {
        fs::create_dir_all(&data_dir).expect("Could not create data directory");
    }
    
    let data_file = data_dir.join("tasks.json");
    
    match &cli.command {
        Commands::Add { description } => add_task(description, &data_file),
        Commands::List => list_tasks(&data_file),
        Commands::Complete { id } => complete_task(*id, &data_file),
        Commands::Remove { id } => remove_task(*id, &data_file),
    }
}

fn load_tasks(data_file: &PathBuf) -> Vec<Task> {
    if !data_file.exists() {
        return vec![];
    }
    
    let content = fs::read_to_string(data_file).expect("Could not read data file");
    serde_json::from_str(&content).unwrap_or_else(|_| vec![])
}

fn save_tasks(tasks: &[Task], data_file: &PathBuf) {
    let json = serde_json::to_string_pretty(tasks).expect("Could not serialize tasks");
    fs::write(data_file, json).expect("Could not write to data file");
}

fn add_task(description: &str, data_file: &PathBuf) {
    let mut tasks = load_tasks(data_file);
    
    let new_id = tasks.iter().map(|task| task.id).max().unwrap_or(0) + 1;
    let new_task = Task {
        id: new_id,
        description: description.to_string(),
        completed: false,
    };
    
    tasks.push(new_task);
    save_tasks(&tasks, data_file);
    
    println!("Added task: {}", description);
}

fn list_tasks(data_file: &PathBuf) {
    let tasks = load_tasks(data_file);
    
    if tasks.is_empty() {
        println!("No tasks found.");
        return;
    }
    
    for task in tasks {
        let status = if task.completed { "[x]" } else { "[ ]" };
        println!("{} {}: {}", status, task.id, task.description);
    }
}

fn complete_task(id: usize, data_file: &PathBuf) {
    let mut tasks = load_tasks(data_file);
    
    if let Some(task) = tasks.iter_mut().find(|task| task.id == id) {
        task.completed = true;
        save_tasks(&tasks, data_file);
        println!("Completed task #{}", id);
    } else {
        println!("Task #{} not found.", id);
    }
}

fn remove_task(id: usize, data_file: &PathBuf) {
    let mut tasks = load_tasks(data_file);
    
    let initial_len = tasks.len();
    tasks.retain(|task| task.id != id);
    
    if tasks.len() == initial_len {
        println!("Task #{} not found.", id);
    } else {
        save_tasks(&tasks, data_file);
        println!("Removed task #{}", id);
    }
}