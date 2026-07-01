use serde_json::json;
use std::fs;
use std::io;
use std::path::PathBuf;

use crate::todo::Todo;

pub struct Storage {
    data_file: PathBuf,
}

impl Storage {
    pub fn new(data_file: PathBuf) -> Self {
        Self { data_file }
    }

    pub fn load(&self) -> Vec<Todo> {
        if !self.data_file.exists() {
            return vec![];
        }

        match fs::read_to_string(&self.data_file) {
            Ok(content) => match serde_json::from_str::<serde_json::Value>(&content) {
                Ok(serde_json::Value::Array(items)) => items
                    .iter()
                    .filter_map(|item| item.as_object().and_then(|m| Todo::from_dict(m).ok()))
                    .collect(),
                Ok(_) => {
                    eprintln!("[storage] data file is not a JSON array, attempting recovery");
                    self.recover_or_init()
                }
                Err(err) => {
                    eprintln!("[storage] JSON decode error: {}, attempting recovery", err);
                    self.recover_or_init()
                }
            },
            Err(err) => {
                eprintln!("[storage] could not read data file: {}, attempting recovery", err);
                self.recover_or_init()
            }
        }
    }

    pub fn save(&self, todos: &[Todo]) -> io::Result<()> {
        let values: Vec<serde_json::Value> = todos.iter().map(|t| t.to_dict()).map(json).collect();
        let content = serde_json::to_string_pretty(&values)?;

        let parent = self
            .data_file
            .parent()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "data file has no parent dir"))?;
        fs::create_dir_all(parent)?;

        let temp_path = parent.join(format!(".tasks.json.tmp.{}", std::process::id()));
        fs::write(&temp_path, content)?;
        fs::rename(&temp_path, &self.data_file)?;
        Ok(())
    }

    fn recover_or_init(&self) -> Vec<Todo> {
        let backup = self.data_file.with_extension("json.bak");
        if backup.exists() {
            match fs::read_to_string(&backup) {
                Ok(content) => match serde_json::from_str::<serde_json::Value>(&content) {
                    Ok(serde_json::Value::Array(items)) => {
                        eprintln!("[storage] recovered from backup");
                        return items
                            .iter()
                            .filter_map(|item| item.as_object().and_then(|m| Todo::from_dict(m).ok()))
                            .collect();
                    }
                    _ => eprintln!("[storage] backup file is also invalid"),
                },
                Err(err) => eprintln!("[storage] could not read backup file: {}", err),
            }
        }
        eprintln!("[storage] initializing empty todo list");
        vec![]
    }
}
