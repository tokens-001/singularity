use std::fs;
use std::path::PathBuf;
use todo_cli::cli;
use todo_cli::storage::Storage;
use todo_cli::todo::Todo;

#[test]
fn test_full_workflow() {
    let temp_dir = tempfile::tempdir().expect("Could not create temporary directory");
    let data_file = temp_dir.path().join("tasks.json");

    // Simulate adding a task by directly using storage
    let storage = Storage::new(data_file.clone());
    let todo = Todo::new("Integration test task").with_id(1);
    let todos = vec![todo];
    storage.save(&todos).expect("Could not save tasks");

    // Verify the task was saved
    let loaded_todos = storage.load();
    assert_eq!(loaded_todos.len(), 1);
    assert_eq!(loaded_todos[0].title, "Integration test task");
    assert_eq!(loaded_todos[0].id, Some(1));
    
    // Test that file exists and contains valid JSON
    assert!(data_file.exists());
    let content = fs::read_to_string(&data_file).expect("Could not read data file");
    assert!(content.starts_with("["));
    assert!(content.contains("Integration test task"));
}