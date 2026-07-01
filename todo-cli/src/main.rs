use std::path::PathBuf;

use todo_cli::cli;

fn main() {
    let data_dir = dirs::data_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("todo-cli");
    let data_file = data_dir.join("tasks.json");

    cli::run(data_file);
}
