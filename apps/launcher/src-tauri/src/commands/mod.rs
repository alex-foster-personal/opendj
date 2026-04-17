//! Tauri commands exposed to the React frontend.
//!
//! Each submodule exposes `#[tauri::command]` functions registered via
//! `invoke_handler![...]` in `main.rs`. Keeping them in small focused files
//! makes unit tests easy (the actual command fns are thin wrappers over
//! plain Rust fns that do not need the Tauri runtime).

pub mod drag;
pub mod frecency;
pub mod hotkey;
pub mod search;
pub mod window;
