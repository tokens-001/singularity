use std::fs;
use std::path::Path;

use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize, Serialize)]
pub struct Task {
    pub id: u32,
    pub description: String,
    pub completed: bool,
}

pub mod cli;