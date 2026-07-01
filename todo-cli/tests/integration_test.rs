use std::fs;
use tempfile::TempDir;
use todo_cli::cli;

#[test]
fn test_add_and_list_tasks() {
    let temp_dir = TempDir::new().unwrap();
    let data_file = temp_dir.path().join("tasks.json");
    let data_file_str = data_file.to_str().unwrap();

    // Add a task
    cli::add_task_for_test(data_file_str, "Test task");

    // Verify task was added
    let tasks = cli::load_tasks_for_test(data_file_str);
    assert_eq!(tasks.len(), 1);
    assert_eq!(tasks[0].description, "Test task");
    assert_eq!(tasks[0].completed, false);
}

#[test]
fn test_complete_task() {
    let temp_dir = TempDir::new().unwrap();
    let data_file = temp_dir.path().join("tasks.json");
    let data_file_str = data_file.to_str().unwrap();

    // Add a task first
    cli::add_task_for_test(data_file_str, "Test task");

    // Load tasks to get the ID
    let mut tasks = cli::load_tasks_for_test(data_file_str);
    assert_eq!(tasks.len(), 1);
    let task_id = tasks[0].id;

    // Complete the task
    cli::complete_task_for_test(data_file_str, task_id);

    // Verify task was completed
    tasks = cli::load_tasks_for_test(data_file_str);
    assert_eq!(tasks.len(), 1);
    assert_eq!(tasks[0].id, task_id);
    assert_eq!(tasks[0].completed, true);
}

#[test]
fn test_delete_task() {
    let temp_dir = TempDir::new().unwrap();
    let data_file = temp_dir.path().join("tasks.json");
    let data_file_str = data_file.to_str().unwrap();

    // Add a task first
    cli::add_task_for_test(data_file_str, "Test task");

    // Load tasks to get the ID
    let mut tasks = cli::load_tasks_for_test(data_file_str);
    assert_eq!(tasks.len(), 1);
    let task_id = tasks[0].id;

    // Delete the task
    cli::delete_task_for_test(data_file_str, task_id);

    // Verify task was deleted
    tasks = cli::load_tasks_for_test(data_file_str);
    assert_eq!(tasks.len(), 0);
}