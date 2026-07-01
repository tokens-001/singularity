use clap::Parser;
use std::fs;
use std::path::Path;

use crate::Task;

#[derive(Parser)]
#[command(name = "todo")]
#[command(about = "A simple todo CLI", long_about = None)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Commands,
}

#[derive(clap::Subcommand)]
pub enum Commands {
    /// Add a new task
    Add { description: String },
    /// List all tasks
    List,
    /// Complete a task
    Complete { id: u32 },
    /// Remove a task
    Remove { id: u32 },
}

pub fn run(data_file: std::path::PathBuf) {
    let cli = Cli::parse();
    match &cli.command {
        Commands::Add { description } => add_task(description, &data_file),
        Commands::List => list_tasks(&data_file),
        Commands::Complete { id } => complete_task(*id, &data_file),
        Commands::Remove { id } => remove_task(*id, &data_file),
    }
}

fn add_task(description: &str, data_file: &Path) {
    let mut tasks = load_tasks(data_file);
    let id = tasks.iter().map(|t| t.id).max().unwrap_or(0) + 1;
    let task = Task {
        id,
        description: description.to_string(),
        completed: false,
    };
    tasks.push(task);
    save_tasks(&tasks, data_file);
    println!("Added task: {}", description);
}

fn list_tasks(data_file: &Path) {
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

fn complete_task(id: u32, data_file: &Path) {
    let mut tasks = load_tasks(data_file);
    let mut updated = false;
    
    for task in &mut tasks {
        if task.id == id {
            task.completed = true;
            updated = true;
            break;
        }
    }
    
    if updated {
        save_tasks(&tasks, data_file);
        println!("Completed task: {}", id);
    } else {
        println!("Task {} not found.", id);
    }
}

fn remove_task(id: u32, data_file: &Path) {
    let mut tasks = load_tasks(data_file);
    let initial_len = tasks.len();
    tasks.retain(|task| task.id != id);
    
    if tasks.len() < initial_len {
        save_tasks(&tasks, data_file);
        println!("Removed task: {}", id);
    } else {
        println!("Task {} not found.", id);
    }
}

fn load_tasks(data_file: &Path) -> Vec<Task> {
    if !data_file.exists() {
        return Vec::new();
    }
    
    let content = fs::read_to_string(data_file).expect("Failed to read tasks file");
    serde_json::from_str(&content).unwrap_or_else(|_| Vec::new())
}

fn save_tasks(tasks: &[Task], data_file: &Path) {
    let data_dir = data_file.parent().expect("Data file should have parent directory");
    fs::create_dir_all(data_dir).expect("Failed to create data directory");
    
    let content = serde_json::to_string_pretty(tasks).expect("Failed to serialize tasks");
    fs::write(data_file, content).expect("Failed to write tasks file");
}