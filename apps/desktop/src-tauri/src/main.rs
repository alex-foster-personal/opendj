// Open DJ desktop shell entry point.
//
// THIN SHELL ONLY. This binary owns no application logic. It creates one
// window, points it at the bundled bootstrap page, and tells that page
// which engine origin to look for. Everything a user sees once the engine
// is reachable is served by the engine over HTTP.
//
// Why the window is built here rather than declared in tauri.conf.json:
// the bootstrap page needs the engine origin BEFORE its own scripts run,
// and `initialization_script` is the only hook with that guarantee. Window
// creation is explicitly the one thing Rust owns (see apps/desktop/README.md
// "Rust owns the window, nothing else"), so this stays inside the rule.
//
// No `#[tauri::command]` handlers are registered here and none should be
// added. No HTTP client, no database, no product decisions.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{WebviewUrl, WebviewWindowBuilder};

/// The engine address a shipped build looks for. Single source of truth
/// for the shell; the bootstrap page renders whatever it is told, so this
/// value is always visible to the user rather than assumed silently.
///
/// Baked at COMPILE time so two lanes installed on one Mac do not both
/// default to the same port and quietly open each other's engine. The
/// `just dmg` recipe supplies it from `.env`; build.rs re-runs when it
/// changes. Unset means the port the engine itself defaults to.
const DEFAULT_ENGINE_ORIGIN: &str = match option_env!("OPENDJ_DEFAULT_ENGINE_ORIGIN") {
    Some(origin) => origin,
    None => "http://127.0.0.1:8685",
};

/// Test seam, mirroring the repo's ENGINE_CMD seam: point a packaged build
/// at an engine on any loopback port without rebuilding it.
const ENGINE_ORIGIN_ENV: &str = "OPENDJ_ENGINE_ORIGIN";

const WINDOW_LABEL: &str = "main";
const WINDOW_WIDTH: f64 = 1280.0;
const WINDOW_HEIGHT: f64 = 800.0;

/// Read the engine origin from the environment, falling back to the stated
/// default. A variable that is set but blank is an operator mistake and
/// stops the launch rather than silently reverting to the default.
fn engine_origin() -> String {
    match std::env::var(ENGINE_ORIGIN_ENV) {
        Err(_) => DEFAULT_ENGINE_ORIGIN.to_string(),
        Ok(value) if value.trim().is_empty() => panic!(
            "{ENGINE_ORIGIN_ENV} is set but empty; give a full origin such as \
             {DEFAULT_ENGINE_ORIGIN}, or unset it to use that default"
        ),
        Ok(value) => value.trim().to_string(),
    }
}

fn main() {
    let origin = engine_origin();

    tauri::Builder::default()
        .setup(move |app| {
            // serde_json does the escaping, so an origin containing quotes
            // cannot break out of the assignment.
            let script = format!(
                "globalThis.OPENDJ_ENGINE_ORIGIN = {};",
                serde_json::to_string(&origin)?
            );
            // The window title IS productName, so the per-lane `--config`
            // overlay labels the window without a second place to edit.
            let title = app.package_info().name.clone();
            WebviewWindowBuilder::new(
                app,
                WINDOW_LABEL,
                WebviewUrl::App("index.html".into()),
            )
            .title(title)
            .inner_size(WINDOW_WIDTH, WINDOW_HEIGHT)
            .initialization_script(script)
            .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Open DJ desktop shell");
}
