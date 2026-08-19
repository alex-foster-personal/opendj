// Open DJ desktop shell entry point.
//
// THIN SHELL, NOW WITH A LIFECYCLE. This binary owns no application logic:
// no `#[tauri::command]` handlers, no HTTP client for the API, no database,
// no product decisions. What it gained is the one responsibility a shipped
// app cannot outsource -- starting the engine that lives inside its own
// bundle, and stopping it again.
//
// Before this train the artifact was a window onto an engine somebody else
// had started from a repo checkout. That made it a demo. The bar it now has
// to clear is a machine with no repo: the engine, its interpreter, its
// dependencies and the built SPA all ship inside Contents/Resources/payload,
// and the shell picks a free port, starts them, waits for health and points
// the webview at the result.
//
// Window creation stays in Rust for the reason it always did: the bootstrap
// page needs the engine origin BEFORE its own scripts run, and
// `initialization_script` is the only hook with that guarantee.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod engine;

use std::path::PathBuf;
use std::sync::Mutex;

use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

/// Test seam, mirroring the repo's ENGINE_CMD seam: point a packaged build
/// at an engine that is already running, on any loopback port, without
/// rebuilding it. Set means "do not start a sidecar".
const ENGINE_ORIGIN_ENV: &str = "OPENDJ_ENGINE_ORIGIN";

const WINDOW_LABEL: &str = "main";
const WINDOW_WIDTH: f64 = 1280.0;
const WINDOW_HEIGHT: f64 = 800.0;

/// The engine's log, inside the app's own data directory so a tester can be
/// asked for one path rather than talked through Console.app.
const ENGINE_LOG: &str = "logs/engine.log";

// ----- build identity -----------------------------------------------------
// Baked at COMPILE time by the `dmg` recipe. `option_env!` returns None for a
// plain `cargo build`, and an unstamped shell says so rather than inventing a
// sha: the whole point of this readout is that a build which cannot describe
// itself must look different from one that can.
const BUILD_GIT_SHA: Option<&str> = option_env!("OPENDJ_BUILD_GIT_SHA");
const BUILD_GIT_SHA_FULL: Option<&str> = option_env!("OPENDJ_BUILD_GIT_SHA_FULL");
const BUILD_GIT_BRANCH: Option<&str> = option_env!("OPENDJ_BUILD_GIT_BRANCH");
const BUILD_GIT_DIRTY: Option<&str> = option_env!("OPENDJ_BUILD_GIT_DIRTY");
const BUILD_AT_UTC: Option<&str> = option_env!("OPENDJ_BUILD_AT_UTC");
const BUILD_LANE_LABEL: Option<&str> = option_env!("OPENDJ_BUILD_LANE_LABEL");

/// The shell's own identity, injected for the UI to render beside the
/// engine's. The two can drift -- a shell pointed at a dev engine is exactly
/// that -- and until now there was no way to see it.
fn shell_build_identity(app_version: &str) -> serde_json::Value {
    serde_json::json!({
        "stamped": BUILD_GIT_SHA.is_some(),
        "app_version": app_version,
        "git_sha": BUILD_GIT_SHA,
        "git_sha_full": BUILD_GIT_SHA_FULL,
        "git_branch": BUILD_GIT_BRANCH,
        "git_dirty": BUILD_GIT_DIRTY.map(|value| value == "1" || value == "true"),
        "built_at_utc": BUILD_AT_UTC,
        "lane_label": BUILD_LANE_LABEL,
    })
}

// ----- supervisor ---------------------------------------------------------
/// The engine handle, so `RunEvent::Exit` can stop what `setup` started.
struct Supervisor(Mutex<Option<engine::Engine>>);

impl Supervisor {
    fn shutdown(&self) {
        if let Ok(mut guard) = self.0.lock() {
            if let Some(mut running) = guard.take() {
                running.shutdown();
            }
        }
    }
}

/// Where the installed app keeps its library.
///
/// `app_data_dir()` is `~/Library/Application Support/<bundle identifier>`,
/// and the identifier carries the lane suffix, so lane A and lane B on one
/// Mac cannot see each other's data. It is never the repo's `data/` and
/// never a real rekordbox library: the engine only creates what it owns
/// (state/, jobs.db, the lock) and imports happen through the setup flow.
fn app_data_dir(app: &AppHandle) -> Result<PathBuf, engine::EngineError> {
    app.path().app_data_dir().map_err(|err| engine::EngineError {
        headline: "Open DJ could not find its data folder.".into(),
        detail: format!("Resolving Application Support failed: {err}"),
    })
}

fn payload_dir(app: &AppHandle) -> Result<PathBuf, engine::EngineError> {
    let resources = app.path().resource_dir().map_err(|err| engine::EngineError {
        headline: "Open DJ could not find its own resources.".into(),
        detail: format!("Resolving the bundle resource directory failed: {err}"),
    })?;
    Ok(resources.join(engine::PAYLOAD_DIR))
}

/// Start the bundled engine and wait for it to be healthy.
///
/// Synchronous, before the window exists, on purpose. A native modal on
/// macOS must run on the main thread, and the failure path here MUST raise
/// one: the alternative is the blank window this whole design exists to
/// eliminate. Waiting costs the measured ~1.5s of a cold boot.
fn start_engine(app: &AppHandle) -> Result<engine::Engine, engine::EngineError> {
    let payload = payload_dir(app)?;
    let data_dir = app_data_dir(app)?;
    let log_path = data_dir.join(ENGINE_LOG);
    let port = engine::free_loopback_port()?;
    let mut running = engine::spawn(&payload, &data_dir, &log_path, port)?;
    match running.wait_until_healthy(engine::BOOT_TIMEOUT) {
        Ok(()) => Ok(running),
        Err(mut failure) => {
            let tail = engine::log_tail(&log_path, 20);
            if !tail.is_empty() {
                failure.detail = format!("{}\n\nLast engine output:\n{tail}", failure.detail);
            }
            running.shutdown();
            Err(failure)
        }
    }
}

/// The only place this shell ever gives up, and it does so loudly.
fn fail_visibly(error: &engine::EngineError) -> ! {
    rfd::MessageDialog::new()
        .set_level(rfd::MessageLevel::Error)
        .set_title("Open DJ cannot start")
        .set_description(format!("{}\n\n{}", error.headline, error.detail))
        .set_buttons(rfd::MessageButtons::Ok)
        .show();
    eprintln!("[ERROR] {error}");
    std::process::exit(1);
}

fn main() {
    let app = tauri::Builder::default()
        .setup(move |app| {
            let handle = app.handle().clone();

            // An externally supplied origin means an operator is driving this
            // build against an engine they started. Honour it exactly, and do
            // not start a second engine behind their back.
            let (origin, running) = match std::env::var(ENGINE_ORIGIN_ENV) {
                Ok(value) if value.trim().is_empty() => panic!(
                    "{ENGINE_ORIGIN_ENV} is set but empty; give a full \
                     loopback origin including the port, or unset it to let \
                     the app start its own bundled engine"
                ),
                Ok(value) => (value.trim().to_string(), None),
                Err(_) => match start_engine(&handle) {
                    Ok(started) => (started.origin(), Some(started)),
                    Err(failure) => fail_visibly(&failure),
                },
            };

            let package = app.package_info();
            let identity = shell_build_identity(&package.version.to_string());
            // serde_json does the escaping, so neither the origin nor the
            // identity can break out of its assignment.
            let script = format!(
                "globalThis.OPENDJ_ENGINE_ORIGIN = {};\
                 globalThis.OPENDJ_SHELL_BUILD = {};",
                serde_json::to_string(&origin)?,
                serde_json::to_string(&identity)?,
            );
            // The window title IS productName, so the per-lane `--config`
            // overlay labels the window without a second place to edit.
            let title = package.name.clone();
            WebviewWindowBuilder::new(app, WINDOW_LABEL, WebviewUrl::App("index.html".into()))
                .title(title)
                .inner_size(WINDOW_WIDTH, WINDOW_HEIGHT)
                .initialization_script(script)
                .build()?;

            app.manage(Supervisor(Mutex::new(running)));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Open DJ desktop shell");

    app.run(|handle, event| {
        // Exit, not ExitRequested: the engine must outlive a closed window
        // only until the process itself is going away, and the kill is a
        // process-group kill so no job the engine spawned is left holding
        // the data directory's lock.
        if let RunEvent::Exit = event {
            if let Some(supervisor) = handle.try_state::<Supervisor>() {
                supervisor.shutdown();
            }
        }
    });
}
