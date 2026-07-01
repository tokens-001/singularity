use clap::{Parser, Subcommand};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;

#[derive(Debug, Deserialize, Serialize)]
struct Task {
    id: usize,
    description: String,
    completed: bool,
}

#[derive(Parser)]
#[command(name = "todo")]
#[command(about = "A simple CLI tool for managing todos", long_about = None)]
struct Cli {
    #[arg(short, long, value_name = "FILE")]
    data_file: String,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Add a new task
    Add { description: String },
    /// List all tasks
    List,
    /// Mark a task as completed
    Complete { id: usize },
    /// Delete a task
    Delete { id: usize },
}

pub fn run(data_file: String) {
    let cli = Cli::parse();
    
    match &cli.command {
        Commands::Add { description } => add_task(&cli.data_file, description),
        Commands::List => list_tasks(&cli.data_file),
        Commands::Complete { id } => complete_task(&cli.data_file, *id),
        Commands::Delete { id } => delete_task(&cli.data_file, *id),
    }
}

fn load_tasks(data_file: &str) -> Vec<Task> {
    if !Path::new(data_file).exists() {
        return Vec::new();
    }

    let content = fs::read_to_string(data_file).expect("Failed to read data file");
    if content.trim().is_empty() {
        return Vec::new();
    }

    serde_json::from_str(&content).expect("Failed to parse data file")
}

fn save_tasks(data_file: &str, tasks: &[Task]) {
    let json = serde_json::to_string_pretty(tasks).expect("Failed to serialize tasks");
    fs::write(data_file, json).expect("Failed to write to data file");
}

fn add_task(data_file: &str, description: &str) {
    let mut tasks = load_tasks(data_file);
    
    let new_id = if tasks.is_empty() {
        1
    } else {
        tasks.iter().map(|task| task.id).max().unwrap_or(0) + 1
    };

    let new_task = Task {
        id: new_id,
        description: description.to_string(),
        completed: false,
    };

    tasks.push(new_task);
    save_tasks(data_file, &tasks);
    
    println!("Added task: {}", description);
}

fn list_tasks(data_file: &str) {
    let tasks = load_tasks(data_file);

    if tasks.is_empty() {
        println!("No tasks found.");
        return;
    }

    for task in tasks {
        let status = if task.completed { "✓" } else { "○" };
        println!("[{}] {}: {}", status, task.id, task.description);
    }
}

fn complete_task(data_file: &str, id: usize) {
    let mut tasks = load_tasks(data_file);
    
    if let Some(task) = tasks.iter_mut().find(|task| task.id == id) {
        task.completed = true;
        save_tasks(data_file, &tasks);
        println!("Completed task: {} - {}", id, task.description);
    } else {
        println!("Task with ID {} not found.", id);
    }
}

fn delete_task(data_file: &str, id: usize) {
    let mut tasks = load_tasks(data_file);
    let initial_len = tasks.len();
    
    tasks.retain(|task| task.id != id);
    
    if tasks.len() == initial_len {
        println!("Task with ID {} not found.", id);
    } else {
        save_tasks(data_file, &tasks);
        println!("Deleted task with ID: {}", id);
    }
}