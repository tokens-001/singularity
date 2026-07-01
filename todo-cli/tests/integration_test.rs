use std::fs;
use tempfile::TempDir;
use todo_cli::cli;
use todo_cli::storage::Storage;
use todo_cli::todo::Todo;

#[test]
fn test_add_and_list_todos() {
    let temp_dir = TempDir::new().unwrap();
    let data_file = temp_dir.path().join("tasks.json");
    let storage = Storage::new(data_file.clone());

    // Add a todo
    let todo = Todo::new("Test task").with_id(1);
    let mut todos = storage.load();
    todos.push(todo);
    storage.save(&todos).unwrap();

    // Verify it was saved correctly
    let loaded_todos = storage.load();
    assert_eq!(loaded_todos.len(), 1);
    assert_eq!(loaded_todos[0].title, "Test task");
    assert_eq!(loaded_todos[0].completed, false);
}

#[test]
fn test_complete_todo() {
    let temp_dir = TempDir::new().unwrap();
    let data_file = temp_dir.path().join("tasks.json");
    let storage = Storage::new(data_file.clone());

    // Add a todo
    let todo = Todo::new("Test task").with_id(1);
    let mut todos = storage.load();
    todos.push(todo);
    storage.save(&todos).unwrap();

    // Complete the todo
    let mut todos = storage.load();
    if let Some(t) = todos.iter_mut().find(|t| t.id == Some(1)) {
        t.completed = true;
    }
    storage.save(&todos).unwrap();

    // Verify it was updated
    let loaded_todos = storage.load();
    assert_eq!(loaded_todos.len(), 1);
    assert_eq!(loaded_todos[0].title, "Test task");
    assert_eq!(loaded_todos[0].completed, true);
}

#[test]
fn test_remove_todo() {
    let temp_dir = TempDir::new().unwrap();
    let data_file = temp_dir.path().join("tasks.json");
    let storage = Storage::new(data_file.clone());

    // Add two todos
    let todo1 = Todo::new("Test task 1").with_id(1);
    let todo2 = Todo::new("Test task 2").with_id(2);
    let mut todos = storage.load();
    todos.push(todo1);
    todos.push(todo2);
    storage.save(&todos).unwrap();

    // Remove one todo
    let mut todos = storage.load();
    todos.retain(|t| t.id != Some(1));
    storage.save(&todos).unwrap();

    // Verify only one remains
    let loaded_todos = storage.load();
    assert_eq!(loaded_todos.len(), 1);
    assert_eq!(loaded_todos[0].title, "Test task 2");
}