//! Hyper-K launcher library crate.
//!
//! Exposes the modules that both `src/main.rs` (Tauri app binary) and the
//! `examples/search_bench.rs` latency benchmark depend on. Tauri commands in
//! `commands::*` are `#[tauri::command]`-annotated; the pure helpers
//! (validate_drag_path, build_fts_query, get_frecent_top_impl, record_drag_impl,
//! search_tracks_impl, decay, raw_score) are the parts exercised by
//! `cargo test`.

pub mod commands;
pub mod state;
