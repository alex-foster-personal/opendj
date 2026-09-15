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
//
// The four `#[tauri::command]` fns are referenced by their FULL path inline
// in `generate_handler!` below, not imported by bare name here. Tauri's
// command macro expands each fn into a sibling hidden macro
// (`__cmd__<name>`) at the SAME module path; `generate_handler!` looks that
// sibling up via the identifier it was given, so a bare-name `use` (which
// only brings the fn's value-namespace item into scope, not the
// differently-named macro-namespace one) leaves it unresolvable across the
// bin/lib crate boundary -- `error: cannot find macro '__cmd__<name>' in
// this scope` for every command, every time (issue #2759 packaging CI: this
// was latent because nothing had ever compiled `launcher` end to end
// before). Fully qualifying the path in `generate_handler!` is Tauri's own
// documented fix for multi-module commands.
use launcher::commands::{
    hotkey::{maybe_show_first_run_notification, register_hotkey, tray_tooltip},
    window::toggle_palette_visibility,
};
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
            launcher::commands::drag::start_track_drag,
            launcher::commands::search::search_tracks,
            launcher::commands::frecency::get_frecent_top,
            launcher::commands::frecency::record_drag,
        ])
        .run(tauri::generate_context!())
        .expect("error while running Hyper-K launcher");
}
