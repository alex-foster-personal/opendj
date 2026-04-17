// Hyper-K Launcher entry point (Phase 17).
//
// Menu-bar-resident Tauri app. Alt+Space toggles a floating cmdk palette that
// drags tracks into djay Pro. Phase 18 layers multi-vendor drag dispatch on
// top; search backend (FTS5) is in `commands::search` + `commands::frecency`.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// All real code lives in the library crate (src/lib.rs); this binary is a
// thin entry point so `search_bench` can share the same modules.
//
// Phase 18 owns `src/drag/` in the *library* crate (declared under
// `#[cfg(any())]` in lib.rs for now); the dispatcher is wired into
// invoke_handler! in a Phase 17/18 gap-fill pass (see
// .planning/phases/17-*/17-03-SUMMARY.md follow-ups).
use launcher::commands::{drag::start_track_drag, frecency::{get_frecent_top, record_drag},
               hotkey::register_hotkey, search::search_tracks, window::toggle_palette_visibility};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager,
};

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_drag::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_store::Builder::new().build())
        .setup(|app| {
            // Register global hotkey.
            let handle = app.handle();
            let _active = register_hotkey(handle).unwrap_or_else(|e| {
                eprintln!("hotkey register failed: {e}; palette reachable via tray only");
                String::new()
            });

            // Build a minimal tray with a "Toggle Palette" item.
            let toggle_item = MenuItem::with_id(handle, "toggle", "Toggle Palette", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(handle, "quit", "Quit Hyper-K", true, None::<&str>)?;
            let menu = Menu::with_items(handle, &[&toggle_item, &quit_item])?;
            let _tray = TrayIconBuilder::new()
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "toggle" => toggle_palette_visibility(app),
                    "quit" => app.exit(0),
                    _ => (),
                })
                .on_tray_icon_event(|tray, event| {
                    if let tauri::tray::TrayIconEvent::Click { button, .. } = event {
                        if matches!(button, tauri::tray::MouseButton::Left) {
                            toggle_palette_visibility(tray.app_handle());
                        }
                    }
                })
                .build(handle)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            start_track_drag,
            search_tracks,
            get_frecent_top,
            record_drag,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Hyper-K launcher");
}
