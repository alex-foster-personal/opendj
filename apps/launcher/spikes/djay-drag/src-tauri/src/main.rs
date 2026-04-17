// Phase 17 Plan 01 -- djay drag spike.
//
// Minimal Tauri 2 app exposing a single command `start_track_drag(path)` that
// initiates a native NSDraggingSession via `tauri-plugin-drag`. Used only to
// verify djay Pro accepts the drag payload. Production code lives in
// `apps/launcher/src-tauri/` once the spike signs off.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;

#[tauri::command]
async fn start_track_drag(window: tauri::WebviewWindow, path: String) -> Result<(), String> {
    use tauri_plugin_drag::{DragItem, Image};
    if path.is_empty() {
        return Err("path is empty".into());
    }
    let p = PathBuf::from(&path);
    let icon_bytes = include_bytes!("../icons/128x128.png");
    tauri_plugin_drag::start_drag(
        &window,
        DragItem::Files(vec![p]),
        Image::Raw(icon_bytes.to_vec()),
        |_result| {},
        Default::default(),
    )
    .map_err(|e| e.to_string())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_drag::init())
        .invoke_handler(tauri::generate_handler![start_track_drag])
        .run(tauri::generate_context!())
        .expect("error while running djay-drag-spike");
}
