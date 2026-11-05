//! WINSTATE-01: the main window reopens at the size and position last
//! used on the monitor it opens on, across restarts AND app updates.
//!
//! WHY NOT `tauri-plugin-window-state`: the industry-standard plugin keeps ONE
//! geometry per window label, so a laptop-then-desk-monitor user gets the last
//! screen's geometry squeezed onto the other one. the maintainer's ask is per monitor
//! ("the window size last used for that monitor / screen"). The plugin's
//! save-on-move and atomic-file behaviour is mimicked here instead.
//!
//! WHERE: `<app data dir>/window-state.json`, next to `.engine.lock`. The app
//! bundle in /Applications is replaced on every update; Application Support
//! is not, so the file survives updates by construction.
//!
//! WHEN: on every move or resize, debounced by `SAVE_DEBOUNCE`, and once more
//! synchronously at exit. Exit-only saving loses the state whenever the
//! updater's quit-and-swap (or a crash, or a SIGKILL) ends the process first.
//!
//! The decision logic is pure (`placement_for_launch`, `record_geometry`) so it
//! is unit tested without a display. Coordinates are LOGICAL points, stored
//! relative to the monitor's work-area origin so a monitor that moves in the
//! arrangement still gets its window back on itself.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};

pub const WINDOW_STATE_FILE: &str = "window-state.json";
pub const WINDOW_STATE_VERSION: u32 = 1;
/// Mirrors tauri-plugin-window-state's practical cadence: quiet for this long
/// after the last move/resize event, then write once.
const SAVE_DEBOUNCE: Duration = Duration::from_millis(500);
/// Never restore a window smaller than this; a near-zero saved size is a
/// broken file, not a preference.
pub const MIN_WINDOW_W_PT: f64 = 480.0;
pub const MIN_WINDOW_H_PT: f64 = 320.0;

// ----- data -----------------------------------------------------------------

/// A rectangle in logical points.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

/// One monitor as the shell sees it at launch or at save time.
#[derive(Clone, Debug, PartialEq)]
pub struct MonitorInfo {
    pub key: String,
    /// Usable area (menu bar and Dock excluded) in global logical points.
    pub work_area: Rect,
    pub is_primary: bool,
}

/// What was last used on one monitor. Offsets are from that monitor's
/// work-area origin.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct SavedGeometry {
    pub x_offset_pt: f64,
    pub y_offset_pt: f64,
    pub width_pt: f64,
    pub height_pt: f64,
    pub maximized: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct WindowStateFile {
    pub version: u32,
    pub last_monitor: Option<String>,
    pub monitors: BTreeMap<String, SavedGeometry>,
}

impl Default for WindowStateFile {
    fn default() -> Self {
        Self {
            version: WINDOW_STATE_VERSION,
            last_monitor: None,
            monitors: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PlacementSource {
    /// Geometry saved for this monitor, clamped to its current work area.
    Saved,
    /// No saved geometry for this monitor: the default size, centred.
    Default,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Placement {
    pub monitor_key: String,
    pub rect: Rect,
    pub maximized: bool,
    pub source: PlacementSource,
}

// ----- pure logic ------------------------------------------------------------

/// Stable monitor identity: name plus physical resolution plus scale factor.
/// Two identical panels on one desk share a key; that is accepted, they would
/// want the same geometry anyway.
pub fn monitor_key(name: Option<&str>, width_px: u32, height_px: u32, scale: f64) -> String {
    let label = name
        .map(str::trim)
        .filter(|n| !n.is_empty())
        .unwrap_or("unnamed");
    format!("{label}|{width_px}x{height_px}|@{scale:.2}")
}

/// Pick the monitor the window opens on and the geometry to open it with.
///
/// Target monitor: the last-used one if it is still connected, else the
/// primary, else the first reported. Returns `None` only when no monitor is
/// reported at all, in which case the caller opens unclamped.
pub fn placement_for_launch(
    saved: &WindowStateFile,
    monitors: &[MonitorInfo],
    default_size_pt: (f64, f64),
) -> Option<Placement> {
    let last = saved
        .last_monitor
        .as_deref()
        .and_then(|key| monitors.iter().find(|m| m.key == key));
    let target = last
        .or_else(|| monitors.iter().find(|m| m.is_primary))
        .or_else(|| monitors.first())?;
    let wa = target.work_area;
    match saved.monitors.get(&target.key) {
        Some(geometry) => {
            let wanted = Rect {
                x: wa.x + geometry.x_offset_pt,
                y: wa.y + geometry.y_offset_pt,
                w: geometry.width_pt,
                h: geometry.height_pt,
            };
            Some(Placement {
                monitor_key: target.key.clone(),
                rect: clamp_to_work_area(wanted, wa),
                maximized: geometry.maximized,
                source: PlacementSource::Saved,
            })
        }
        None => {
            let (w, h) = default_size_pt;
            let centred = Rect {
                x: wa.x + (wa.w - w) / 2.0,
                y: wa.y + (wa.h - h) / 2.0,
                w,
                h,
            };
            Some(Placement {
                monitor_key: target.key.clone(),
                rect: clamp_to_work_area(centred, wa),
                maximized: false,
                source: PlacementSource::Default,
            })
        }
    }
}

/// Shrink to fit the work area (never below the minimum unless the work area
/// itself is smaller), then slide fully inside it.
pub fn clamp_to_work_area(wanted: Rect, wa: Rect) -> Rect {
    let w = wanted.w.max(MIN_WINDOW_W_PT).min(wa.w);
    let h = wanted.h.max(MIN_WINDOW_H_PT).min(wa.h);
    let x = wanted.x.max(wa.x).min(wa.x + wa.w - w);
    let y = wanted.y.max(wa.y).min(wa.y + wa.h - h);
    Rect { x, y, w, h }
}

/// Record the window's current geometry against the monitor it is on.
///
/// While maximized, the normal-size geometry already saved for that monitor is
/// kept (only the flag flips), so un-maximizing after a restart returns to the
/// size the user actually chose.
pub fn record_geometry(
    state: &mut WindowStateFile,
    monitor: &MonitorInfo,
    window: Rect,
    maximized: bool,
) {
    let wa = monitor.work_area;
    let current = SavedGeometry {
        x_offset_pt: window.x - wa.x,
        y_offset_pt: window.y - wa.y,
        width_pt: window.w,
        height_pt: window.h,
        maximized,
    };
    let entry = match (maximized, state.monitors.get(&monitor.key)) {
        (true, Some(previous)) => SavedGeometry {
            maximized: true,
            ..*previous
        },
        (true, None) => current,
        (false, _) => current,
    };
    state.monitors.insert(monitor.key.clone(), entry);
    state.last_monitor = Some(monitor.key.clone());
}

// ----- file I/O --------------------------------------------------------------

pub fn window_state_path(data_dir: &Path) -> PathBuf {
    data_dir.join(WINDOW_STATE_FILE)
}

/// `Ok(None)` when the file does not exist yet (first launch). A file that
/// exists but cannot be parsed is an `Err` naming the path, so the caller logs
/// it loudly; the window then opens at the default size and the next save
/// overwrites the broken file.
pub fn load_window_state(path: &Path) -> Result<Option<WindowStateFile>, String> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(format!("{}: {err}", path.display())),
    };
    let parsed: WindowStateFile =
        serde_json::from_str(&text).map_err(|err| format!("{}: {err}", path.display()))?;
    if parsed.version != WINDOW_STATE_VERSION {
        return Err(format!(
            "{}: version {} is not the supported {WINDOW_STATE_VERSION}",
            path.display(),
            parsed.version
        ));
    }
    Ok(Some(parsed))
}

/// Atomic write: temp file in the same directory, then rename.
pub fn save_window_state(path: &Path, state: &WindowStateFile) -> Result<(), String> {
    let body = serde_json::to_string_pretty(state).map_err(|err| format!("serialize: {err}"))?;
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, body).map_err(|err| format!("{}: {err}", tmp.display()))?;
    std::fs::rename(&tmp, path).map_err(|err| format!("{}: {err}", path.display()))
}

// ----- debounced store -------------------------------------------------------

/// Managed Tauri state: the in-memory window state plus a debounced writer.
pub struct WindowStateStore {
    path: PathBuf,
    state: Arc<Mutex<WindowStateFile>>,
    changed: Sender<()>,
}

impl WindowStateStore {
    /// Start the in-process writer thread. `log` receives every write failure.
    pub fn start(path: PathBuf, initial: WindowStateFile, log: fn(&str, &str)) -> Self {
        let state = Arc::new(Mutex::new(initial));
        let (changed, events) = mpsc::channel::<()>();
        let writer_state = Arc::clone(&state);
        let writer_path = path.clone();
        std::thread::Builder::new()
            .name("window-state-writer".into())
            .spawn(move || {
                while events.recv().is_ok() {
                    loop {
                        match events.recv_timeout(SAVE_DEBOUNCE) {
                            Ok(()) => continue,
                            Err(RecvTimeoutError::Timeout) => break,
                            Err(RecvTimeoutError::Disconnected) => break,
                        }
                    }
                    let snapshot = writer_state.lock().expect("window state mutex").clone();
                    if let Err(failure) = save_window_state(&writer_path, &snapshot) {
                        log("WARN", &format!("window state not saved: {failure}"));
                    }
                }
            })
            .expect("start window-state writer thread");
        Self {
            path,
            state,
            changed,
        }
    }

    pub fn record(&self, monitor: &MonitorInfo, window: Rect, maximized: bool) {
        record_geometry(
            &mut self.state.lock().expect("window state mutex"),
            monitor,
            window,
            maximized,
        );
        // The writer only stops when this store is dropped, so a send failure
        // cannot happen while `self` is alive.
        let _ = self.changed.send(());
    }

    /// Synchronous write for the exit path, where the debounce may not fire.
    pub fn flush(&self) -> Result<(), String> {
        let snapshot = self.state.lock().expect("window state mutex").clone();
        save_window_state(&self.path, &snapshot)
    }
}

// ----- tauri glue ------------------------------------------------------------

/// Convert a Tauri monitor to the pure-logic view. Physical work-area pixels
/// divided by that monitor's own scale factor give global logical points.
pub fn monitor_info(monitor: &tauri::Monitor, is_primary: bool) -> MonitorInfo {
    let scale = monitor.scale_factor();
    let size = monitor.size();
    let area = monitor.work_area();
    MonitorInfo {
        key: monitor_key(
            monitor.name().map(String::as_str),
            size.width,
            size.height,
            scale,
        ),
        work_area: Rect {
            x: f64::from(area.position.x) / scale,
            y: f64::from(area.position.y) / scale,
            w: f64::from(area.size.width) / scale,
            h: f64::from(area.size.height) / scale,
        },
        is_primary,
    }
}

/// Every connected monitor, primary flagged. Errors are returned, not hidden:
/// the caller logs them and opens at the default size.
pub fn connected_monitors(app: &tauri::AppHandle) -> Result<Vec<MonitorInfo>, String> {
    let primary_key = app
        .primary_monitor()
        .map_err(|err| format!("primary monitor: {err}"))?
        .map(|m| monitor_info(&m, true).key);
    let monitors = app
        .available_monitors()
        .map_err(|err| format!("available monitors: {err}"))?;
    Ok(monitors
        .iter()
        .map(|m| {
            let info = monitor_info(m, false);
            let is_primary = primary_key.as_deref() == Some(info.key.as_str());
            MonitorInfo { is_primary, ..info }
        })
        .collect())
}

/// Snapshot the window's geometry and hand it to the store. Skipped while
/// minimized or fullscreen: neither is a size the user wants back at launch.
pub fn record_window(window: &tauri::Window, store: &WindowStateStore) -> Result<(), String> {
    let minimized = window
        .is_minimized()
        .map_err(|err| format!("is minimized: {err}"))?;
    let fullscreen = window
        .is_fullscreen()
        .map_err(|err| format!("is fullscreen: {err}"))?;
    if minimized || fullscreen {
        return Ok(());
    }
    let Some(monitor) = window
        .current_monitor()
        .map_err(|err| format!("current monitor: {err}"))?
    else {
        return Ok(());
    };
    let scale = window
        .scale_factor()
        .map_err(|err| format!("scale factor: {err}"))?;
    let position = window
        .outer_position()
        .map_err(|err| format!("outer position: {err}"))?;
    let size = window
        .inner_size()
        .map_err(|err| format!("inner size: {err}"))?;
    let maximized = window
        .is_maximized()
        .map_err(|err| format!("is maximized: {err}"))?;
    let rect = Rect {
        x: f64::from(position.x) / scale,
        y: f64::from(position.y) / scale,
        w: f64::from(size.width) / scale,
        h: f64::from(size.height) / scale,
    };
    store.record(&monitor_info(&monitor, false), rect, maximized);
    Ok(())
}

// ----- tests -----------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    const DEFAULT: (f64, f64) = (2035.0, 1449.0);

    fn laptop() -> MonitorInfo {
        MonitorInfo {
            key: monitor_key(Some("Built-in Retina Display"), 3024, 1964, 2.0),
            work_area: Rect {
                x: 0.0,
                y: 25.0,
                w: 1512.0,
                h: 920.0,
            },
            is_primary: true,
        }
    }

    fn desk() -> MonitorInfo {
        MonitorInfo {
            key: monitor_key(Some("LG UltraFine"), 3840, 2160, 1.5),
            work_area: Rect {
                x: 1512.0,
                y: 0.0,
                w: 2560.0,
                h: 1415.0,
            },
            is_primary: false,
        }
    }

    fn saved_on(monitor: &MonitorInfo, geometry: SavedGeometry) -> WindowStateFile {
        let mut state = WindowStateFile::default();
        state.monitors.insert(monitor.key.clone(), geometry);
        state.last_monitor = Some(monitor.key.clone());
        state
    }

    fn geometry(x: f64, y: f64, w: f64, h: f64) -> SavedGeometry {
        SavedGeometry {
            x_offset_pt: x,
            y_offset_pt: y,
            width_pt: w,
            height_pt: h,
            maximized: false,
        }
    }

    #[test]
    fn restores_the_geometry_saved_for_the_last_used_monitor() {
        let state = saved_on(&desk(), geometry(100.0, 50.0, 1800.0, 1100.0));
        let placement =
            placement_for_launch(&state, &[laptop(), desk()], DEFAULT).expect("placement");
        assert_eq!(
            placement.source,
            PlacementSource::Saved,
            "if the saved desk geometry is ignored then broken"
        );
        assert_eq!(placement.monitor_key, desk().key);
        assert_eq!(
            placement.rect,
            Rect {
                x: 1612.0,
                y: 50.0,
                w: 1800.0,
                h: 1100.0
            }
        );
    }

    #[test]
    fn each_monitor_keeps_its_own_geometry() {
        let mut state = saved_on(&desk(), geometry(100.0, 50.0, 1800.0, 1100.0));
        state
            .monitors
            .insert(laptop().key, geometry(10.0, 10.0, 1200.0, 800.0));
        // Desk unplugged: the laptop's own last-used geometry comes back, not the desk's.
        let placement = placement_for_launch(&state, &[laptop()], DEFAULT).expect("placement");
        assert_eq!(
            placement.source,
            PlacementSource::Saved,
            "if per-monitor geometry is shared then broken"
        );
        assert_eq!(
            placement.rect,
            Rect {
                x: 10.0,
                y: 35.0,
                w: 1200.0,
                h: 800.0
            }
        );
    }

    #[test]
    fn a_gone_monitor_falls_back_to_default_centred_on_the_primary() {
        let state = saved_on(&desk(), geometry(100.0, 50.0, 1800.0, 1100.0));
        let placement = placement_for_launch(&state, &[laptop()], DEFAULT).expect("placement");
        assert_eq!(
            placement.source,
            PlacementSource::Default,
            "if a gone monitor's geometry is reused then broken"
        );
        assert_eq!(placement.monitor_key, laptop().key);
        assert_eq!(
            placement.rect,
            Rect {
                x: 0.0,
                y: 25.0,
                w: 1512.0,
                h: 920.0
            }
        );
        let small = placement_for_launch(&state, &[laptop()], (1000.0, 600.0)).expect("placement");
        assert_eq!(
            small.rect,
            Rect {
                x: 256.0,
                y: 185.0,
                w: 1000.0,
                h: 600.0
            },
            "if default is not centred then broken"
        );
    }

    #[test]
    fn saved_geometry_is_clamped_into_a_smaller_work_area() {
        // Same monitor key but the Dock grew: the saved rect overhangs.
        let state = saved_on(&laptop(), geometry(900.0, 600.0, 2000.0, 700.0));
        let placement = placement_for_launch(&state, &[laptop()], DEFAULT).expect("placement");
        assert_eq!(
            placement.rect,
            Rect {
                x: 0.0,
                y: 245.0,
                w: 1512.0,
                h: 700.0
            },
            "if off-screen geometry is restored then broken"
        );
    }

    #[test]
    fn a_degenerate_saved_size_is_raised_to_the_minimum() {
        let state = saved_on(&laptop(), geometry(0.0, 0.0, 3.0, 2.0));
        let placement = placement_for_launch(&state, &[laptop()], DEFAULT).expect("placement");
        assert_eq!(
            (placement.rect.w, placement.rect.h),
            (MIN_WINDOW_W_PT, MIN_WINDOW_H_PT)
        );
    }

    #[test]
    fn no_monitors_means_no_placement() {
        assert_eq!(
            placement_for_launch(&WindowStateFile::default(), &[], DEFAULT),
            None
        );
    }

    #[test]
    fn record_then_launch_round_trips() {
        let mut state = WindowStateFile::default();
        record_geometry(
            &mut state,
            &desk(),
            Rect {
                x: 1700.0,
                y: 80.0,
                w: 1600.0,
                h: 1000.0,
            },
            false,
        );
        assert_eq!(state.last_monitor.as_deref(), Some(desk().key.as_str()));
        let placement =
            placement_for_launch(&state, &[laptop(), desk()], DEFAULT).expect("placement");
        assert_eq!(
            placement.rect,
            Rect {
                x: 1700.0,
                y: 80.0,
                w: 1600.0,
                h: 1000.0
            },
            "if record and restore disagree then broken"
        );
    }

    #[test]
    fn maximizing_keeps_the_normal_geometry() {
        let mut state = WindowStateFile::default();
        let normal = Rect {
            x: 100.0,
            y: 100.0,
            w: 1200.0,
            h: 800.0,
        };
        record_geometry(&mut state, &laptop(), normal, false);
        record_geometry(&mut state, &laptop(), laptop().work_area, true);
        let saved = state.monitors[&laptop().key];
        assert!(saved.maximized);
        assert_eq!(
            (saved.width_pt, saved.height_pt),
            (1200.0, 800.0),
            "if maximize overwrites the normal size then broken"
        );
    }

    #[test]
    fn monitor_key_names_resolution_and_scale() {
        assert_eq!(
            monitor_key(Some("LG"), 3840, 2160, 1.5),
            "LG|3840x2160|@1.50"
        );
        assert_eq!(
            monitor_key(None, 1920, 1080, 1.0),
            "unnamed|1920x1080|@1.00"
        );
        assert_ne!(
            monitor_key(Some("LG"), 3840, 2160, 2.0),
            monitor_key(Some("LG"), 3840, 2160, 1.5)
        );
    }

    #[test]
    fn file_round_trips_and_missing_is_none() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = window_state_path(dir.path());
        assert_eq!(
            load_window_state(&path),
            Ok(None),
            "if a first launch errors then broken"
        );
        let state = saved_on(&desk(), geometry(1.0, 2.0, 1300.0, 900.0));
        save_window_state(&path, &state).expect("save");
        assert_eq!(load_window_state(&path), Ok(Some(state)));
    }

    #[test]
    fn a_corrupt_or_future_file_is_an_error_naming_the_path() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = window_state_path(dir.path());
        std::fs::write(&path, "{not json").expect("write");
        let err = load_window_state(&path).expect_err("corrupt");
        assert!(
            err.contains(WINDOW_STATE_FILE),
            "if a corrupt file is silently ignored then broken: {err}"
        );
        std::fs::write(&path, r#"{"version":99,"last_monitor":null,"monitors":{}}"#)
            .expect("write");
        assert!(load_window_state(&path)
            .expect_err("future")
            .contains("version 99"));
    }
}
