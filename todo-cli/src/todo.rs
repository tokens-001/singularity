use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Todo {
    pub id: Option<usize>,
    pub title: String,
    pub completed: bool,
}

impl Todo {
    pub const MAX_TITLE_LEN: usize = 200;

    pub fn new(title: &str) -> Self {
        Self {
            id: None,
            title: title.to_string(),
            completed: false,
        }
    }

    pub fn with_id(mut self, id: usize) -> Self {
        self.id = Some(id);
        self
    }

    pub fn to_dict(&self) -> serde_json::Map<String, serde_json::Value> {
        let mut map = serde_json::Map::new();
        if let Some(id) = self.id {
            map.insert("id".to_string(), serde_json::Value::Number(id.into()));
        }
        map.insert("title".to_string(), serde_json::Value::String(self.title.clone()));
        map.insert("completed".to_string(), serde_json::Value::Bool(self.completed));
        map
    }

    pub fn from_dict(map: &serde_json::Map<String, serde_json::Value>) -> Result<Self, String> {
        let id = map
            .get("id")
            .map(|v| v.as_u64().map(|n| n as usize))
            .flatten();
        let title = map
            .get("title")
            .and_then(|v| v.as_str())
            .ok_or_else(|| "missing title".to_string())?
            .to_string();
        let completed = map
            .get("completed")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        Ok(Self { id, title, completed })
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.title.trim().is_empty() {
            return Err("title cannot be empty".to_string());
        }
        if self.title.len() > Self::MAX_TITLE_LEN {
            return Err(format!("title cannot exceed {} characters", Self::MAX_TITLE_LEN));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_to_dict_roundtrip() {
        let todo = Todo::new("Buy milk").with_id(1);
        let dict = todo.to_dict();
        let recovered = Todo::from_dict(&dict).unwrap();
        assert_eq!(todo, recovered);
    }

    #[test]
    fn test_from_dict_without_id() {
        let mut map = serde_json::Map::new();
        map.insert("title".to_string(), serde_json::Value::String("Walk dog".to_string()));
        map.insert("completed".to_string(), serde_json::Value::Bool(true));
        let todo = Todo::from_dict(&map).unwrap();
        assert_eq!(todo.id, None);
        assert_eq!(todo.title, "Walk dog");
        assert!(todo.completed);
    }

    #[test]
    fn test_validate_empty_title() {
        let todo = Todo::new("");
        assert!(todo.validate().is_err());
    }

    #[test]
    fn test_validate_whitespace_title() {
        let todo = Todo::new("   ");
        assert!(todo.validate().is_err());
    }

    #[test]
    fn test_validate_long_title() {
        let long_title = "a".repeat(Todo::MAX_TITLE_LEN + 1);
        let todo = Todo::new(&long_title);
        assert!(todo.validate().is_err());
    }

    #[test]
    fn test_validate_valid_title() {
        let todo = Todo::new("Valid title");
        assert!(todo.validate().is_ok());
    }
}
