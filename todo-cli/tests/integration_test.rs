use std::fs;
use std::process::Command;
use tempfile::TempDir;

#[test]
fn test_add_and_list_todos() {
    let temp_dir = TempDir::new().expect("Could not create temporary directory");
    let data_file = temp_dir.path().join("tasks.json");

    // Add a todo
    let output = Command::new("cargo")
        .args(["run", "--", "add", "Integration test task"])
        .env("TODO_DATA_FILE", &data_file)
        .output()
        .expect("Failed to execute command");
    
    assert!(output.status.success());
    assert!(String::from_utf8_lossy(&output.stdout).contains("Added todo"));

    // List todos
    let output = Command::new("cargo")
        .args(["run", "--", "list"])
        .env("TODO_DATA_FILE", &data_file)
        .output()
        .expect("Failed to execute command");
    
    assert!(output.status.success());
    assert!(String::from_utf8_lossy(&output.stdout).contains("Integration test task"));
}

#[test]
fn test_complete_todo() {
    let temp_dir = TempDir::new().expect("Could not create temporary directory");
    let data_file = temp_dir.path().join("tasks.json");

    // Add a todo
    let output = Command::new("cargo")
        .args(["run", "--", "add", "Task to complete"])
        .env("TODO_DATA_FILE", &data_file)
        .output()
        .expect("Failed to execute command");
    
    assert!(output.status.success());

    // Complete the todo (assuming it gets ID 1)
    let output = Command::new("cargo")
        .args(["run", "--", "complete", "1"])
        .env("TODO_DATA_FILE", &data_file)
        .output()
        .expect("Failed to execute command");
    
    assert!(output.status.success());
    assert!(String::from_utf8_lossy(&output.stdout).contains("Completed todo #1"));

    // Verify it's marked as completed
    let output = Command::new("cargo")
        .args(["run", "--", "list"])
        .env("TODO_DATA_FILE", &data_file)
        .output()
        .expect("Failed to execute command");
    
    assert!(output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("[x] 1 Task to complete"));
}

#[test]
fn test_remove_todo() {
    let temp_dir = TempDir::new().expect("Could not create temporary directory");
    let data_file = temp_dir.path().join("tasks.json");

    // Add a todo
    let output = Command::new("cargo")
        .args(["run", "--", "add", "Task to remove"])
        .env("TODO_DATA_FILE", &data_file)
        .output()
        .expect("Failed to execute command");
    
    assert!(output.status.success());

    // Remove the todo
    let output = Command::new("cargo")
        .args(["run", "--", "remove", "1"])
        .env("TODO_DATA_FILE", &data_file)
        .output()
        .expect("Failed to execute command");
    
    assert!(output.status.success());
    assert!(String::from_utf8_lossy(&output.stdout).contains("Removed todo #1"));

    // Verify it's gone
    let output = Command::new("cargo")
        .args(["run", "--", "list"])
        .env("TODO_DATA_FILE", &data_file)
        .output()
        .expect("Failed to execute command");
    
    assert!(output.status.success());
    assert!(!String::from_utf8_lossy(&output.stdout).contains("Task to remove"));
}

#[test]
fn test_list_empty_todos() {
    let temp_dir = TempDir::new().expect("Could not create temporary directory");
    let data_file = temp_dir.path().join("tasks.json");

    let output = Command::new("cargo")
        .args(["run", "--", "list"])
        .env("TODO_DATA_FILE", &data_file)
        .output()
        .expect("Failed to execute command");
    
    assert!(output.status.success());
    assert!(String::from_utf8_lossy(&output.stdout).contains("No todos found."));
}