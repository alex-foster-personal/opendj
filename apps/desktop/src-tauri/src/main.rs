// Open DJ desktop shell entry point.
//
// THIN SHELL ONLY. This binary owns no application logic: the single window
// loads the engine's web UI directly over HTTP (see tauri.conf.json
// `app.windows[].url`, currently hardcoded to http://127.0.0.1:8683 --
// TODO: read the engine port from config instead of hardcoding it).
//
// No Tauri IPC commands are registered here and none should be added. If a
// desktop-native capability is ever needed (file dialogs, tray icon, etc.)
// it still must not become a home for app/business logic -- that lives in
// the engine. See apps/desktop/README.md for the full rule.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running Open DJ desktop shell");
}
