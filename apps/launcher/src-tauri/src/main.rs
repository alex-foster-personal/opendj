// Hyper-K Launcher entry point (Phase 17 + Phase 18 wiring).
//
// Menu-bar-resident Tauri app. Alt+Space toggles a floating cmdk palette that
// drags tracks into the currently-running DJ app. Multi-vendor drag dispatch
// lives in the `launcher-drag-core` crate (see `apps/launcher/drag-core/`);
// `commands::drag::TauriHostBridge` is the production HostBridge impl that
// wires the `drag` crate, `tauri-plugin-clipboard-manager`, and the macOS
// NSWorkspace running-apps probe into the dispatcher. Search backend (FTS5)
// is in `commands::search` + `commands::frecency`.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// All real code lives in the library crate (src/lib.rs); this binary is a
// thin entry point so `search_bench` can share the same modules.
use launcher::commands::{drag::start_track_drag, frecency::{get_frecent_top, record_drag},
               hotkey::{maybe_show_first_run_notification, register_hotkey, tray_tooltip},
               search::search_tracks, window::toggle_palette_visibility};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager,
};

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_drag::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_store::Builder::new().build())
        .setup(|app| {
            // Register global hotkey. `active` carries the binding that
            // actually succeeded (PRIMARY or FALLBACK), which downstream
            // UX surfaces (tray tooltip + first-run notification) need to
            // stay truthful about what the user should press.
            let handle = app.handle();
            let active = register_hotkey(handle).unwrap_or_else(|e| {
                eprintln!("hotkey register failed: {e}; palette reachable via tray only");
                String::new()
            });

            // First-run discoverability: show a one-shot system notification
            // telling the user which hotkey opens the palette. Idempotent --
            // the flag lives in SQLite so it fires exactly once per profile.
            // Only fires when we have a live binding; otherwise there is no
            // hotkey to advertise.
            if !active.is_empty() {
                maybe_show_first_run_notification(handle, &active);
            }

            // Build a minimal tray with a "Toggle Palette" item. The tooltip
            // includes the active hotkey so hovering the tray icon answers
            // the discoverability question too (belt + braces with the
            // first-run notification above).
            let toggle_item = MenuItem::with_id(handle, "toggle", "Toggle Palette", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(handle, "quit", "Quit Hyper-K", true, None::<&str>)?;
            let menu = Menu::with_items(handle, &[&toggle_item, &quit_item])?;
            let mut tray_builder = TrayIconBuilder::new()
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
                });
            if !active.is_empty() {
                tray_builder = tray_builder.tooltip(tray_tooltip(&active));
            }
            let _tray = tray_builder.build(handle)?;
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
