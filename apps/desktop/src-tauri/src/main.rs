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

/// The size the window opens at, in LOGICAL POINTS -- not pixels. On a 2x
/// Retina display these are half the physical pixel count, which is the unit
/// `inner_size` takes and the unit CSS sees.
///
/// Sized for the app's own layout (decks, waveforms and browser side by side)
/// rather than for any particular screen: `starting_window_size` shrinks it to
/// whatever monitor it actually opens on, so a number larger than a laptop
/// display is a target, not a bug.
const STARTING_WINDOW_W_PT: f64 = 2035.0;
const STARTING_WINDOW_H_PT: f64 = 1449.0;

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

// ----- window size --------------------------------------------------------
/// The monitor's usable area in LOGICAL POINTS, or None when it cannot be read.
///
/// Tauri reports monitor geometry in PHYSICAL pixels, so the raw numbers are
/// twice the points on a 2x display; dividing by the monitor's scale factor
/// puts them in the same unit as `STARTING_WINDOW_*_PT`. Comparing the two
/// without that divide is how a 1449pt-tall window "fits" a 900pt-tall screen.
///
/// `work_area`, not `size`: the usable region excludes the macOS menu bar and
/// the Dock, and a window that opens underneath the Dock is not a window that
/// fits.
///
/// The scale factor is checked rather than trusted. `to_logical` would have
/// done the divide, but it asserts on a non-positive factor, and a panic here
/// is the blank window this shell exists to eliminate.
fn usable_area_pt(app: &AppHandle) -> Option<(f64, f64)> {
    let monitor = match app.primary_monitor() {
        Ok(Some(found)) => found,
        Ok(None) => {
            eprintln!("[WARN] no primary monitor reported; opening unclamped");
            return None;
        }
        Err(err) => {
            eprintln!("[WARN] could not read the primary monitor ({err}); opening unclamped");
            return None;
        }
    };
    let scale = monitor.scale_factor();
    if !scale.is_finite() || scale <= 0.0 {
        eprintln!("[WARN] monitor reported scale factor {scale}; opening unclamped");
        return None;
    }
    let usable = monitor.work_area().size;
    Some((
        f64::from(usable.width) / scale,
        f64::from(usable.height) / scale,
    ))
}

/// The configured starting size, shrunk to fit the monitor it opens on.
///
/// Per dimension, NOT aspect-preserving: a window too wide for a narrow
/// monitor should lose width and keep its height.
///
/// `None` means no monitor could be resolved, and the configured size is then
/// applied unchanged. There is nothing to clamp against, and the OS constrains
/// an oversize window on its own, so inventing a fallback screen size would be
/// a guess that could only be wrong.
fn starting_window_size(usable_pt: Option<(f64, f64)>) -> (f64, f64) {
    match usable_pt {
        None => (STARTING_WINDOW_W_PT, STARTING_WINDOW_H_PT),
        Some((usable_w, usable_h)) => (
            STARTING_WINDOW_W_PT.min(usable_w),
            STARTING_WINDOW_H_PT.min(usable_h),
        ),
    }
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
    let builder = tauri::Builder::default();

    // DEBUG BUILDS ONLY. The dependency itself is gated on cfg(debug_assertions)
    // in Cargo.toml, so a release build cannot compile this line in at all.
    // It registers the embedded WebDriver server that lets a real-shell e2e
    // drive this window on macOS. It adds NO #[tauri::command] handlers and its
    // permission set is empty, so the rule above holds exactly as written.
    #[cfg(debug_assertions)]
    let builder = builder.plugin(tauri_plugin_wdio_webdriver::init());

    let app = builder
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
            let (width, height) = starting_window_size(usable_area_pt(&handle));
            WebviewWindowBuilder::new(app, WINDOW_LABEL, WebviewUrl::App("index.html".into()))
                .title(title)
                .inner_size(width, height)
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

#[cfg(test)]
mod tests {
    use super::*;

    // - if the clamp stops shrinking then the window opens taller than the
    //   screen and its own title bar is off the top -> broken
    // - if it clamps both dimensions when only one overflows then a wide
    //   monitor gets a needlessly short window -> broken
    // - if an unresolvable monitor shrinks to some assumed screen size then
    //   the app opens small for a reason nobody can see -> broken

    /// A 2560x1440 monitor with the menu bar and Dock taken out, in points.
    const ROOMY: (f64, f64) = (2560.0, 1440.0);
    /// A 14-inch MacBook Pro's usable area in points, which both configured
    /// dimensions exceed.
    const LAPTOP: (f64, f64) = (1512.0, 916.0);

    #[test]
    fn a_monitor_smaller_than_the_target_shrinks_both_dimensions() {
        assert_eq!(starting_window_size(Some(LAPTOP)), LAPTOP);
    }

    #[test]
    fn only_the_dimension_that_overflows_is_clamped() {
        // Wide enough, not tall enough: the width must survive intact.
        let (width, height) = starting_window_size(Some(ROOMY));
        assert_eq!(width, STARTING_WINDOW_W_PT);
        assert_eq!(height, ROOMY.1);
    }

    #[test]
    fn a_monitor_big_enough_for_the_target_leaves_it_alone() {
        assert_eq!(
            starting_window_size(Some((4000.0, 3000.0))),
            (STARTING_WINDOW_W_PT, STARTING_WINDOW_H_PT)
        );
    }

    #[test]
    fn an_unresolvable_monitor_applies_the_configured_size_unchanged() {
        assert_eq!(
            starting_window_size(None),
            (STARTING_WINDOW_W_PT, STARTING_WINDOW_H_PT)
        );
    }
}
