// Open DJ desktop shell entry point.
//
// THIN SHELL, NOW WITH A LIFECYCLE AND NATIVE TRANSPORTS. This binary owns no
// deck or library logic, no HTTP client for the API, and no database. It owns
// the responsibilities a shipped app cannot outsource: starting/stopping the
// bundled engine and bridging OS hardware APIs that WKWebView does not expose.
// Native MIDI carries raw bytes only; TypeScript still owns all controller
// mapping and behavior.
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

mod cue_sink;
mod engine;
mod engine_log;
mod launch;
mod midi;
mod output_health;
mod shell_health;
mod supervisor;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use tauri::webview::PageLoadEvent;
use tauri::{
    AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};

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
const PARENT_FILE: &str = ".engine.parent";

// ----- diagnostic flags ----------------------------------------------------
// `payload/bin/opendj` and `payload/bin/opendj-engine` are argparse programs:
// `--help`/`-h` prints usage and exits before touching anything else. This
// shell has no argparse, but an agent probing it with the same flag must get
// the same contract, not a second GUI instance (issue #2868). Checked before
// `install_signal_handlers`, the single-instance plugin, or anything that
// resolves the data dir, so a diagnostic probe truly touches nothing.
const USAGE: &str = "\
usage: opendj-desktop [-h] [--version]

Open DJ desktop shell -- boots the bundled engine and opens the app window.
Run with no arguments to launch normally.

options:
  -h, --help     show this help message and exit
  --version      show the shell's version and exit
";

#[derive(Debug, PartialEq, Eq)]
enum DiagnosticFlag {
    Help,
    Version,
}

/// Pure so the guard is unit-testable: no argv global, no process exit.
fn parse_diagnostic_flag<'a>(args: impl Iterator<Item = &'a str>) -> Option<DiagnosticFlag> {
    for arg in args {
        match arg {
            "-h" | "--help" => return Some(DiagnosticFlag::Help),
            "--version" => return Some(DiagnosticFlag::Version),
            _ => {}
        }
    }
    None
}

/// Print usage or version for a diagnostic flag and report the process exit
/// code, or `None` when the caller should proceed to launch the app.
///
/// `app_version` MUST come from `tauri.conf.json` (via `Context::package_info`),
/// not `CARGO_PKG_VERSION`: INSTALL-07 defines `app_version` as the semver the
/// updater compares, and `Cargo.toml`'s version is a separate, driftable
/// number (0.1.0 there vs. 0.1.1 in `tauri.conf.json` at time of writing).
fn handle_diagnostic_flags<'a>(args: impl Iterator<Item = &'a str>, app_version: &str) -> Option<i32> {
    match parse_diagnostic_flag(args)? {
        DiagnosticFlag::Help => {
            print!("{USAGE}");
            Some(0)
        }
        DiagnosticFlag::Version => {
            println!("opendj-desktop {app_version}");
            Some(0)
        }
    }
}

/// 0 means no signal received yet. Set only from the signal handler itself
/// (an async-signal-safe atomic store), read from the poll thread below,
/// which does the actual logging and shutdown work outside the signal
/// context.
static SIGNAL_RECEIVED: AtomicI32 = AtomicI32::new(0);
static ON_SHUTDOWN: OnceLock<Mutex<Option<Box<dyn Fn() + Send + Sync>>>> = OnceLock::new();

/// Name a signal this shell installs a handler for. The handler only ever
/// delivers SIGTERM or SIGINT, so "unknown" would mean the atomic was
/// corrupted, not a real third signal.
fn signal_name(signal: i32) -> &'static str {
    if signal == libc::SIGTERM {
        "SIGTERM"
    } else if signal == libc::SIGINT {
        "SIGINT"
    } else {
        "unknown signal"
    }
}

/// Name a `RunEvent::ExitRequested`'s trigger from its `code` field.
///
/// `None` is what tauri reports for a quit the platform delivers directly to
/// the app -- Cmd-Q, the Dock menu, or an Apple Event `quit` such as
/// `osascript ... to quit` -- because none of those originate as a call this
/// process made on itself. `Some(_)` is this process asking to exit itself,
/// which for this shell means the webview's confirmed quit flow calling the
/// process plugin's `exit()`.
fn exit_requested_trigger(code: Option<i32>) -> String {
    match code {
        None => "apple-event-quit".to_string(),
        Some(code) => format!("programmatic-exit(code={code})"),
    }
}

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
const BUILD_CHANNEL: Option<&str> = option_env!("OPENDJ_BUILD_CHANNEL");
const BUILD_EVIDENCE_AT_UTC: Option<&str> = option_env!("OPENDJ_BUILD_EVIDENCE_AT_UTC");

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
        "release_channel": BUILD_CHANNEL,
        "evidence_written_at_utc": BUILD_EVIDENCE_AT_UTC,
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
            engine::append_shell_log("WARN", "no primary monitor reported; opening unclamped");
            return None;
        }
        Err(err) => {
            engine::append_shell_log(
                "WARN",
                &format!("could not read the primary monitor ({err}); opening unclamped"),
            );
            return None;
        }
    };
    let scale = monitor.scale_factor();
    if !scale.is_finite() || scale <= 0.0 {
        engine::append_shell_log(
            "WARN",
            &format!("monitor reported scale factor {scale}; opening unclamped"),
        );
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

use supervisor::{start_runtime_supervisor, Supervised, SupervisorPaths, RuntimeSupervisorState};

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

fn write_parent_file(data_dir: &Path) -> Result<(), engine::EngineError> {
    let path = data_dir.join(PARENT_FILE);
    std::fs::write(&path, format!("{}\n", std::process::id())).map_err(|err| {
        engine::EngineError::new(
            "Open DJ could not record its shell parent.",
            format!("{}: {err}", path.display()),
        )
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).map_err(|err| {
            engine::EngineError::new(
                "Open DJ could not secure its shell parent file.",
                format!("{}: {err}", path.display()),
            )
        })?;
    }
    Ok(())
}

fn show_stop_or_quit_dialog(pid: u32, detail: &str, lock_path: &Path) -> bool {
    let body = format!(
        "Another Open DJ engine (pid {pid}) already holds the lock at \
         {lock_path}.\n\n{detail}\n\nStop that engine and start a new one, \
         or quit.",
        lock_path = lock_path.display()
    );
    rfd::MessageDialog::new()
        .set_level(rfd::MessageLevel::Error)
        .set_title("Open DJ cannot start")
        .set_description(body)
        .set_buttons(rfd::MessageButtons::OkCancelCustom(
            "Stop engine".to_string(),
            "Quit".to_string(),
        ))
        .show()
        == rfd::MessageDialogResult::Ok
}

/// Start or adopt the engine and wait for a healthy origin.
///
/// Synchronous, before the window exists, on purpose. A native modal on
/// macOS must run on the main thread, and the failure path here MUST raise
/// one: the alternative is the blank window this whole design exists to
/// eliminate. Waiting costs the measured ~1.5s of a cold boot.
fn start_engine(app: &AppHandle) -> Result<Supervised, engine::EngineError> {
    let payload = payload_dir(app)?;
    let data_dir = app_data_dir(app)?;
    let log_path = data_dir.join(ENGINE_LOG);
    let lock_path = launch::lock_path(&data_dir);
    match launch::inspect_lock(&lock_path, engine::health_ok) {
        LaunchPlan::Adopt { pid, host, port } => {
            write_parent_file(&data_dir)?;
            if !engine::health_ok(port) {
                return Err(engine::EngineError::new(
                    "Open DJ could not adopt the running engine.",
                    format!(
                        "pid {pid} holds the lock but is not answering health \
                         at port {port}"
                    ),
                ));
            }
            Ok(Supervised::Adopted { pid, host, port })
        }
        LaunchPlan::StopOrQuit { pid, detail } => {
            if show_stop_or_quit_dialog(pid, &detail, &lock_path) {
                launch::stop_holder_pid(pid);
                spawn_fresh_engine(&payload, &data_dir, &log_path)
            } else {
                Err(engine::EngineError::new("Open DJ could not start.", detail))
            }
        }
        LaunchPlan::Spawn => spawn_fresh_engine(&payload, &data_dir, &log_path),
    }
}

fn spawn_fresh_engine(
    payload: &Path,
    data_dir: &Path,
    log_path: &Path,
) -> Result<Supervised, engine::EngineError> {
    write_parent_file(data_dir)?;
    let port = engine::free_loopback_port()?;
    let mut running = engine::spawn(payload, data_dir, log_path, port)?;
    match running.wait_until_healthy(engine::BOOT_TIMEOUT) {
        Ok(()) => Ok(Supervised::Spawned(running)),
        Err(mut failure) => {
            let lock_path = launch::lock_path(data_dir);
            match launch::inspect_lock(&lock_path, engine::health_ok) {
                LaunchPlan::Adopt { pid, host, port } => {
                    running.shutdown();
                    write_parent_file(data_dir)?;
                    if !engine::health_ok(port) {
                        return Err(engine::EngineError::new(
                            "Open DJ could not adopt the running engine.",
                            format!(
                                "pid {pid} answered the lock file but stopped \
                                 answering health at port {port}"
                            ),
                        ));
                    }
                    return Ok(Supervised::Adopted { pid, host, port });
                }
                LaunchPlan::StopOrQuit { pid, detail } => {
                    running.shutdown();
                    if show_stop_or_quit_dialog(pid, &detail, &lock_path) {
                        launch::stop_holder_pid(pid);
                        return spawn_fresh_engine(payload, data_dir, log_path);
                    }
                    return Err(engine::EngineError::new(
                        "Open DJ could not start.",
                        detail,
                    ));
                }
                LaunchPlan::Spawn => {
                    let tail = engine::log_tail(log_path, 20);
                    if !tail.is_empty() {
                        failure.detail = format!("{}\n\nLast engine output:\n{tail}", failure.detail);
                    }
                    running.shutdown();
                    Err(failure)
                }
            }
        }
    }
}

use launch::LaunchPlan;

fn install_signal_handlers() {
    let _ = ON_SHUTDOWN.set(Mutex::new(None));
    extern "C" fn signal_handler(signal: i32) {
        // Async-signal-safe: one atomic store, nothing else. All the logging
        // and shutdown work happens on the poll thread below, never here.
        SIGNAL_RECEIVED.store(signal, Ordering::SeqCst);
    }
    unsafe {
        libc::signal(libc::SIGTERM, signal_handler as *const () as usize);
        libc::signal(libc::SIGINT, signal_handler as *const () as usize);
    }
    std::thread::spawn(|| {
        let signal = loop {
            let received = SIGNAL_RECEIVED.load(Ordering::SeqCst);
            if received != 0 {
                break received;
            }
            std::thread::sleep(Duration::from_millis(50));
        };
        let name = signal_name(signal);
        engine::append_shell_log("shutdown", &format!("shell exiting: received {name}"));
        if let Some(slot) = ON_SHUTDOWN.get() {
            if let Some(shutdown) = slot.lock().expect("shutdown mutex").as_ref() {
                shutdown();
            }
        }
        engine::append_shell_log("shutdown", &format!("shell exit complete: {name}"));
        std::process::exit(0);
    });
}

/// Second-launch callback for `tauri_plugin_single_instance`: surface the
/// running instance instead of letting a second launch re-run the launch
/// plan against the same data dir and `.engine.lock` (issue #2868). The
/// plugin already killed the second process by the time this runs, so
/// there is no second `Supervised` to shut down here.
fn focus_existing_window(app: &AppHandle, _argv: Vec<String>, _cwd: String) {
    match app.get_webview_window(WINDOW_LABEL) {
        Some(window) => {
            let _ = window.unminimize();
            let _ = window.show();
            let _ = window.set_focus();
            engine::append_shell_log(
                "single-instance",
                "second launch focused the existing window",
            );
        }
        None => engine::append_shell_log(
            "WARN",
            "second launch detected but no existing window to focus",
        ),
    }
}

/// The only place this shell ever gives up, and it does so loudly.
/// INSTALL-21: delegate quit decisions to the engine-served UI. Rust only blocks
/// the OS quit and forwards the request; no product logic lives here.
fn request_quit_from_webview(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(WINDOW_LABEL) {
        let _ = window.eval("globalThis.__OPENDJ_requestQuit?.()");
    }
}

fn fail_visibly(error: &engine::EngineError) -> ! {
    rfd::MessageDialog::new()
        .set_level(rfd::MessageLevel::Error)
        .set_title("Open DJ cannot start")
        .set_description(format!("{}\n\n{}", error.headline, error.detail))
        .set_buttons(rfd::MessageButtons::Ok)
        .show();
    engine::append_shell_log("ERROR", &format!("{error}"));
    std::process::exit(1);
}

fn cue_sink_log(message: &str) {
    engine::append_shell_log("cue-sink", message);
}

fn main() {
    // Generated once and reused for `.build()` below: the macro only reads
    // `tauri.conf.json` at compile time, so calling it here to read the
    // version has no side effect (no window, no plugin, no data dir touched).
    let context = tauri::generate_context!();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let app_version = context.package_info().version.to_string();
    if let Some(code) = handle_diagnostic_flags(args.iter().map(String::as_str), &app_version) {
        std::process::exit(code);
    }

    install_signal_handlers();

    // SINGLE-INSTANCE GUARD (issue #2868). Must be the FIRST plugin
    // registered: the crate's own docs say plugins run in registration
    // order, and this one has to claim the instance lock before anything
    // else runs. Without it, a second launch -- an agent probing the
    // binary, `open -n`, a double-click -- re-runs the whole launch plan in
    // `start_engine` against the SAME `.engine.lock`, which can then Adopt
    // the running engine or SIGTERM/SIGKILL it out from under the user's
    // live session (`launch.rs::stop_holder_pid`). The guard belongs here,
    // at launch admission, not in the lock logic itself.
    let builder = tauri::Builder::default()
        .manage(midi::NativeMidiBridge::default())
        .invoke_handler(tauri::generate_handler![
            midi::platform::native_midi_snapshot,
            midi::platform::native_midi_send
        ])
        .plugin(tauri_plugin_single_instance::init(focus_existing_window))
        // THE AUTO-UPDATE CHANNEL. Registered unconditionally, in release and in
        // debug, so a developer build cannot silently lack the surface a shipped
        // one has. The plugin owns the whole apply path -- fetch, minisign verify
        // against `plugins.updater.pubkey`, swap the bundle -- because signature
        // verification must not be separable from installation. The engine's
        // /api/v1/update/check answers the same question for agents and for a
        // browser tab, and reads the SAME endpoint constant; see docs/auto-update.md
        // for why the check is duplicated rather than shared.
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init());

    // DEBUG BUILDS ONLY. The dependency itself is gated on cfg(debug_assertions)
    // in Cargo.toml, so a release build cannot compile this line in at all.
    // It registers the embedded WebDriver server that lets a real-shell e2e
    // drive this window on macOS. It adds NO #[tauri::command] handlers and its
    // permission set is empty, so the rule above holds exactly as written.
    #[cfg(debug_assertions)]
    let builder = builder.plugin(tauri_plugin_wdio_webdriver::init());

    let app = builder
        .on_window_event(|window, event| {
            if window.label() != WINDOW_LABEL {
                return;
            }
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                engine::append_shell_log(
                    "shutdown",
                    "window-close requested; delegating to webview quit gate",
                );
                request_quit_from_webview(window.app_handle());
            }
        })
        .setup(move |app| {
            let handle = app.handle().clone();
            let data_dir = app_data_dir(&handle).unwrap_or_else(|failure| fail_visibly(&failure));
            let log_path = data_dir.join(ENGINE_LOG);
            if let Err(failure) = engine::install_shell_logging(&log_path) {
                fail_visibly(&failure);
            }

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
            // CUEOUT-22: the native headphone cue output. A failure to start it
            // is logged and leaves the page without the global, so the I/O
            // panel reports two-device cue as unavailable instead of the whole
            // app failing to open over a headphone feature.
            let cue_sink_script = match cue_sink::CueSinkServer::start(cue_sink_log) {
                Ok(server) => server.init_script()?,
                Err(failure) => {
                    cue_sink_log(&failure);
                    String::new()
                }
            };
            // serde_json does the escaping, so neither the origin nor the
            // identity can break out of its assignment.
            let script = format!(
                "globalThis.OPENDJ_ENGINE_ORIGIN = {};\
                 globalThis.OPENDJ_SHELL_BUILD = {};\
                 globalThis.__OPENDJ_requestQuit = globalThis.__OPENDJ_requestQuit || function () {{}};\
                 globalThis.__OPENDJ_PENDING_SHELL_ERRORS__ = globalThis.__OPENDJ_PENDING_SHELL_ERRORS__ || [];\
                 globalThis.__OPENDJ_enqueueShellClientError = function(kind, message, context) {{\
                   globalThis.__OPENDJ_PENDING_SHELL_ERRORS__.push({{\
                     kind: kind,\
                     message: message,\
                     context: context || {{ source: 'shell-webview' }}\
                   }});\
                 }};{}",
                serde_json::to_string(&origin)?,
                serde_json::to_string(&identity)?,
                cue_sink_script,
            );
            // The window title IS productName, so the per-lane `--config`
            // overlay labels the window without a second place to edit.
            let title = package.name.clone();
            let (width, height) = starting_window_size(usable_area_pt(&handle));
            WebviewWindowBuilder::new(app, WINDOW_LABEL, WebviewUrl::App("index.html".into()))
                .title(title)
                .inner_size(width, height)
                .initialization_script(script)
                .on_page_load(|_window, payload| {
                    let url = payload.url().to_string();
                    match payload.event() {
                        PageLoadEvent::Started => {
                            engine::append_shell_log("webview", &format!("navigation started: {url}"));
                        }
                        PageLoadEvent::Finished => {
                            engine::append_shell_log("webview", &format!("navigation finished: {url}"));
                        }
                    }
                })
                .build()?;

            if let Some(started) = running {
                let payload = payload_dir(&handle).unwrap_or_else(|failure| fail_visibly(&failure));
                let paths = SupervisorPaths {
                    payload,
                    data_dir: data_dir.clone(),
                    log_path: log_path.clone(),
                    product_name: package.name.clone(),
                };
                match start_runtime_supervisor(handle.clone(), started, paths, &data_dir) {
                    Ok(runtime) => {
                        if let Some(slot) = ON_SHUTDOWN.get() {
                            let runtime_for_shutdown = Arc::clone(&runtime);
                            *slot.lock().expect("shutdown mutex") = Some(Box::new(move || {
                                runtime_for_shutdown.shutdown();
                            }));
                        }
                        app.manage(RuntimeSupervisorState { supervisor: runtime });
                    }
                    Err(failure) => fail_visibly(&failure),
                }
            } else if let Some(slot) = ON_SHUTDOWN.get() {
                *slot.lock().expect("shutdown mutex") = Some(Box::new(|| {}));
            }

            Ok(())
        })
        .build(context)
        .expect("error while building Open DJ desktop shell");

    app.run(|handle, event| {
        // INSTALL-21: ExitRequested is intercepted and delegated to the web UI.
        // Shutdown runs only on the final Exit after a confirmed quit.
        match event {
            RunEvent::ExitRequested { code, api, .. } => {
                api.prevent_exit();
                engine::append_shell_log(
                    "shutdown",
                    &format!("exit requested: {}", exit_requested_trigger(code)),
                );
                request_quit_from_webview(handle);
            }
            RunEvent::Exit => {
                if let Some(state) = handle.try_state::<RuntimeSupervisorState>() {
                    engine::append_shell_log("shutdown", "shell exit: stopping runtime supervisor");
                    state.supervisor.shutdown();
                    engine::append_shell_log("shutdown", "shell exit: runtime supervisor stopped");
                } else {
                    engine::append_shell_log(
                        "shutdown",
                        "shell exit: no runtime supervisor (external engine origin)",
                    );
                }
            }
            _ => {}
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

    // - if a normal launch (no args, or app-owned args) is read as --help
    //   then the app never opens -> broken
    // - if --help is missed because it is not the FIRST argv item then an
    //   agent's probe still launches a second GUI -> broken (issue #2868)
    // - if -h and --help are not treated identically then usage depends on
    //   which spelling an agent happened to pick -> broken

    fn flag_of(args: &[&str]) -> Option<DiagnosticFlag> {
        parse_diagnostic_flag(args.iter().copied())
    }

    #[test]
    fn no_args_means_launch_the_app() {
        assert_eq!(flag_of(&[]), None);
    }

    #[test]
    fn dash_h_is_help() {
        assert_eq!(flag_of(&["-h"]), Some(DiagnosticFlag::Help));
    }

    #[test]
    fn double_dash_help_is_help() {
        assert_eq!(flag_of(&["--help"]), Some(DiagnosticFlag::Help));
    }

    #[test]
    fn double_dash_version_is_version() {
        assert_eq!(flag_of(&["--version"]), Some(DiagnosticFlag::Version));
    }

    #[test]
    fn help_is_found_even_when_not_the_first_argument() {
        assert_eq!(flag_of(&["--foo", "--help"]), Some(DiagnosticFlag::Help));
    }

    #[test]
    fn an_unrelated_argument_means_launch_the_app() {
        assert_eq!(flag_of(&["--engine-origin-probe"]), None);
    }

    #[test]
    fn handle_diagnostic_flags_reports_exit_zero_for_help() {
        assert_eq!(handle_diagnostic_flags(["--help"].into_iter(), "0.1.1"), Some(0));
    }

    #[test]
    fn handle_diagnostic_flags_reports_exit_zero_for_version() {
        assert_eq!(handle_diagnostic_flags(["--version"].into_iter(), "0.1.1"), Some(0));
    }

    #[test]
    fn handle_diagnostic_flags_returns_none_for_a_normal_launch() {
        assert_eq!(handle_diagnostic_flags(std::iter::empty(), "0.1.1"), None);
    }

    // - if SIGTERM and SIGINT are not named distinctly then a reader of the
    //   shell log cannot tell a script's kill from a terminal Ctrl-C -> broken
    // - if a code:None exit is not named apple-event-quit then a Cmd-Q, a
    //   Dock quit and a raw `osascript ... to quit` are indistinguishable
    //   from this shell asking to exit itself -> broken (issue #2801)

    #[test]
    fn signal_name_distinguishes_sigterm_from_sigint() {
        assert_eq!(signal_name(libc::SIGTERM), "SIGTERM");
        assert_eq!(signal_name(libc::SIGINT), "SIGINT");
        assert_ne!(signal_name(libc::SIGTERM), signal_name(libc::SIGINT));
    }

    #[test]
    fn signal_name_of_an_unhandled_signal_says_so_rather_than_guessing() {
        assert_eq!(signal_name(libc::SIGKILL), "unknown signal");
    }

    #[test]
    fn a_platform_delivered_exit_is_named_apple_event_quit() {
        assert_eq!(exit_requested_trigger(None), "apple-event-quit");
    }

    #[test]
    fn a_self_requested_exit_is_named_programmatic_with_its_code() {
        assert_eq!(exit_requested_trigger(Some(0)), "programmatic-exit(code=0)");
        assert_eq!(exit_requested_trigger(Some(1)), "programmatic-exit(code=1)");
    }
}
