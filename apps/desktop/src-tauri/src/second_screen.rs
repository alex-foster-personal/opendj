//! SECONDSCREEN: the library window on the second display.
//!
//! Window > "Library on Second Screen" (Cmd+Shift+L) and the Cmd K row open
//! `<engine origin>/second-screen` in a second webview window labelled
//! `second-screen`, on the display the main window is not on, with its own
//! per-monitor geometry (`window_state`). A second press focuses the open
//! window instead of opening another. Closing it never reaches the quit gate:
//! the main webview is the audio engine and is not touched.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use serde::Serialize;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{AppHandle, Emitter, Manager, Runtime, Url, WebviewUrl, WebviewWindowBuilder};

use crate::window::{MAIN_WINDOW_LABEL, SECOND_SCREEN_PATH, SECOND_SCREEN_WINDOW_LABEL};
use crate::window_state::{self, WindowStateStore};

pub const SECOND_SCREEN_MENU_ID: &str = "window.second-screen";
pub const SECOND_SCREEN_ACCELERATOR: &str = "CmdOrCtrl+Shift+L";
/// Emitted to `main` after any change to which extra shell windows are open.
/// Payload: `{ "labels": ["second-screen", ...] }` (never `main`; focus does not
/// change the set).
pub const SHELL_WINDOWS_EVENT: &str = "opendj-shell-windows";
const MENU_TEXT: &str = "Library on Second Screen";
// Held only on a worker, never by a menu/UI callback. Serializes creation
// and engine-origin replacement so simultaneous requests open one window.
static WINDOW_LIFECYCLE: Mutex<()> = Mutex::new(());

/// Per-app: set once a close of the open library window is dispatched,
/// cleared when a new one is built. `close()` only queues the close on the UI
/// loop, so the window stays registered until `Destroyed`; without this the
/// labels published right after a close would still list it.
#[derive(Default)]
struct CloseDispatched(AtomicBool);

/// Everything the second window needs that `main()` knows only at setup.
pub struct SecondScreenPage {
    /// The engine origin; the supervisor updates it when the engine moves.
    pub origin: Mutex<String>,
    /// The shell build identity published to the page.
    pub identity: serde_json::Value,
    /// SAME store as the main window, so localStorage is shared.
    pub data_store: [u8; 16],
    pub title: String,
    /// The default window size in logical points.
    pub default_size_pt: (f64, f64),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SecondScreenOutcome {
    Opened,
    Focused,
}

/// `<origin>/second-screen`. Refuses a non-http(s) origin and the port-0
/// placeholder the missing-engine boot uses.
pub fn second_screen_url(origin: &str) -> Result<Url, String> {
    let base = origin.trim().trim_end_matches('/');
    let url: Url = format!("{base}{SECOND_SCREEN_PATH}")
        .parse()
        .map_err(|err| format!("engine origin {origin:?}: {err}"))?;
    if !matches!(url.scheme(), "http" | "https") || url.port_or_known_default() == Some(0) {
        return Err(format!("engine origin {origin:?} is not a running engine"));
    }
    Ok(url)
}

/// The main window's outer position and inner size in global logical points.
fn main_rect_pt<R: Runtime>(window: &tauri::WebviewWindow<R>) -> Result<window_state::Rect, String> {
    let scale = window.scale_factor().map_err(|err| err.to_string())?;
    let position = window.outer_position().map_err(|err| err.to_string())?;
    let size = window.inner_size().map_err(|err| err.to_string())?;
    Ok(window_state::Rect {
        x: f64::from(position.x) / scale,
        y: f64::from(position.y) / scale,
        w: f64::from(size.width) / scale,
        h: f64::from(size.height) / scale,
    })
}

fn focus<R: Runtime>(window: &tauri::WebviewWindow<R>) {
    let _ = window.unminimize();
    let _ = window.show();
    let _ = window.set_focus();
}

/// Worker-only implementation; callers must hold WINDOW_LIFECYCLE.
fn open_or_focus_second_screen_blocking<R: Runtime>(
    app: &AppHandle<R>,
) -> Result<SecondScreenOutcome, String> {
    if let Some(existing) = app.get_webview_window(SECOND_SCREEN_WINDOW_LABEL) {
        focus(&existing);
        crate::engine::append_shell_log("second-screen", "focused existing window");
        return Ok(SecondScreenOutcome::Focused);
    }
    let Some(page) = app.try_state::<SecondScreenPage>() else {
        return Err("the library window needs the engine; it is not running".into());
    };
    let origin = page.origin.lock().expect("second-screen origin").clone();
    let url = second_screen_url(&origin)?;
    let script = crate::shell_globals_script(&origin, &page.identity).map_err(|err| err.to_string())?;

    // Monitors are read only when a geometry store exists to place against:
    // without one the window opens unplaced either way.
    let placement = match app.try_state::<WindowStateStore>() {
        Some(store) => match window_state::connected_monitors(app) {
            Ok(monitors) => {
                let main_rect = app
                    .get_webview_window(MAIN_WINDOW_LABEL)
                    .and_then(|w| main_rect_pt(&w).ok());
                window_state::second_screen_placement(
                    &store.window(SECOND_SCREEN_WINDOW_LABEL),
                    &monitors,
                    main_rect,
                    page.default_size_pt,
                )
            }
            Err(failure) => {
                crate::engine::append_shell_log(
                    "WARN",
                    &format!("second-screen: monitors unreadable, opening unplaced: {failure}"),
                );
                None
            }
        },
        None => None,
    };

    let builder = WebviewWindowBuilder::new(app, SECOND_SCREEN_WINDOW_LABEL, WebviewUrl::External(url))
        .title(&page.title)
        .disable_drag_drop_handler()
        .initialization_script(&script)
        .data_store_identifier(page.data_store);
    let builder = match &placement {
        Some(found) => builder
            .inner_size(found.rect.w, found.rect.h)
            .position(found.rect.x, found.rect.y),
        None => builder.inner_size(page.default_size_pt.0, page.default_size_pt.1),
    };
    // Background throttling stays at the default: this window is not the
    // audio engine.
    let window = match builder.build() {
        Ok(window) => {
            if let Some(closing) = app.try_state::<CloseDispatched>() {
                closing.0.store(false, Ordering::SeqCst);
            }
            window
        }
        Err(err) => {
            if let Some(existing) = app.get_webview_window(SECOND_SCREEN_WINDOW_LABEL) {
                focus(&existing);
                return Ok(SecondScreenOutcome::Focused);
            }
            return Err(format!("second-screen window: {err}"));
        }
    };
    if placement.as_ref().is_some_and(|found| found.maximized) {
        let _ = window.maximize();
    }
    let where_ = placement
        .as_ref()
        .map_or_else(|| "no monitor reported".to_string(), |p| format!("{} from {:?}", p.monitor_key, p.source));
    crate::engine::append_shell_log("second-screen", &format!("opened on {where_}"));
    Ok(SecondScreenOutcome::Opened)
}

/// Run all window lifecycle work off the UI thread. WebView2 creation can
/// deadlock in synchronous commands and event handlers on Windows.
async fn update_second_screen<R: Runtime>(
    app: AppHandle<R>,
    origin: Option<String>,
) -> Result<Option<SecondScreenOutcome>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = WINDOW_LIFECYCLE.lock().map_err(|err| err.to_string())?;
        if let Some(origin) = origin {
            let Some(page) = app.try_state::<SecondScreenPage>() else {
                return Ok(None);
            };
            *page.origin.lock().map_err(|err| err.to_string())? = origin;
            let Some(window) = app.get_webview_window(SECOND_SCREEN_WINDOW_LABEL) else {
                return Ok(None);
            };
            window.destroy().map_err(|err| err.to_string())?;
            // Destruction is dispatched to the UI loop. Wait for Tauri to
            // release the label before reusing it, while that loop stays free.
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
            while app.get_webview_window(SECOND_SCREEN_WINDOW_LABEL).is_some() {
                if std::time::Instant::now() >= deadline {
                    return Err("second-screen window did not finish closing".into());
                }
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
        }
        open_or_focus_second_screen_blocking(&app).map(Some)
    })
    .await
    .map_err(|err| format!("second-screen worker: {err}"))?
}

/// Shared command/menu entry point; creation and focus are serialized.
pub async fn open_or_focus_second_screen<R: Runtime>(
    app: AppHandle<R>,
) -> Result<SecondScreenOutcome, String> {
    update_second_screen(app, None)
        .await?
        .ok_or_else(|| "second-screen window was not opened".into())
}

#[derive(Clone, Serialize)]
struct ShellWindowsPayload {
    labels: Vec<String>,
}

/// Extra window labels that currently exist (never `main`).
pub fn open_extra_window_labels<R: Runtime>(app: &AppHandle<R>) -> Vec<String> {
    let closing = app
        .try_state::<CloseDispatched>()
        .is_some_and(|closing| closing.0.load(Ordering::SeqCst));
    if !closing && app.get_webview_window(SECOND_SCREEN_WINDOW_LABEL).is_some() {
        vec![SECOND_SCREEN_WINDOW_LABEL.into()]
    } else {
        Vec::new()
    }
}

/// Publishes the current extra-window set to the main webview (SECONDSCREEN-10757).
pub fn publish_shell_window_labels<R: Runtime>(app: &AppHandle<R>) {
    let payload = ShellWindowsPayload {
        labels: open_extra_window_labels(app),
    };
    if let Err(err) = app.emit_to(MAIN_WINDOW_LABEL, SHELL_WINDOWS_EVENT, payload) {
        crate::engine::append_shell_log(
            "WARN",
            &format!("opendj-shell-windows emit failed: {err}"),
        );
    }
}

fn unknown_window_label_message(label: &str) -> String {
    format!(
        "unknown window label {label:?}; known: {SECOND_SCREEN_WINDOW_LABEL}"
    )
}

fn parse_window_control(action: &str, label: &str) -> Result<(), String> {
    if label != SECOND_SCREEN_WINDOW_LABEL {
        return Err(unknown_window_label_message(label));
    }
    if action != "open" && action != "close" && action != "focus" {
        return Err(format!(
            "window action must be open, close or focus, got {action}"
        ));
    }
    Ok(())
}

fn close_second_screen_blocking<R: Runtime>(app: &AppHandle<R>) -> Result<(), String> {
    let _guard = WINDOW_LIFECYCLE.lock().map_err(|err| err.to_string())?;
    if let Some(window) = app.get_webview_window(SECOND_SCREEN_WINDOW_LABEL) {
        window.close().map_err(|err| err.to_string())?;
        app.manage(CloseDispatched::default());
        app.state::<CloseDispatched>().0.store(true, Ordering::SeqCst);
        crate::engine::append_shell_log("second-screen", "closed");
    }
    Ok(())
}

#[tauri::command]
pub async fn control_shell_window<R: Runtime>(
    app: AppHandle<R>,
    action: String,
    label: String,
) -> Result<(), String> {
    parse_window_control(&action, &label)?;
    match action.as_str() {
        "open" | "focus" => open_or_focus_second_screen(app.clone()).await.map(|_| ())?,
        "close" => {
            let app_handle = app.clone();
            tauri::async_runtime::spawn_blocking(move || close_second_screen_blocking(&app_handle))
                .await
                .map_err(|err| format!("second-screen worker: {err}"))??;
        }
        _ => unreachable!("validated above"),
    }
    publish_shell_window_labels(&app);
    Ok(())
}

#[tauri::command]
pub fn query_shell_window_labels<R: Runtime>(app: AppHandle<R>) -> Vec<String> {
    open_extra_window_labels(&app)
}

/// The Cmd K row's entry point.
#[tauri::command]
pub async fn open_second_screen<R: Runtime>(app: AppHandle<R>) -> Result<(), String> {
    control_shell_window(
        app,
        "open".into(),
        SECOND_SCREEN_WINDOW_LABEL.into(),
    )
    .await
}

/// The engine moved to a new origin: point the page context at it and reload
/// an open second window there, so it does not keep talking to a dead port.
pub fn follow_engine_origin<R: Runtime>(app: &AppHandle<R>, origin: &str) {
    let app = app.clone();
    let origin = origin.to_string();
    tauri::async_runtime::spawn(async move {
        if let Err(failure) = update_second_screen(app, Some(origin)).await {
            crate::engine::append_shell_log("WARN", &format!("second-screen: reopen failed: {failure}"));
        }
    });
}

/// The default app menu plus Window > Library on Second Screen.
pub fn app_menu<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<Menu<R>> {
    let menu = Menu::default(app)?;
    let item = MenuItem::with_id(app, SECOND_SCREEN_MENU_ID, MENU_TEXT, true, Some(SECOND_SCREEN_ACCELERATOR))?;
    let separator = PredefinedMenuItem::separator(app)?;
    let window_menu = menu
        .items()?
        .into_iter()
        .filter_map(|kind| kind.as_submenu().cloned())
        .find(|sub| sub.text().is_ok_and(|text| text == "Window"));
    match window_menu {
        Some(sub) => {
            sub.append(&separator)?;
            sub.append(&item)?;
        }
        None => {
            let sub = Submenu::new(app, "Window", true)?;
            sub.append(&item)?;
            menu.append(&sub)?;
        }
    }
    Ok(menu)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn page(origin: &str) -> SecondScreenPage {
        SecondScreenPage {
            origin: Mutex::new(origin.into()),
            identity: serde_json::json!({"version": "test"}),
            data_store: [0; 16],
            title: "Open DJ Library".into(),
            default_size_pt: (1200.0, 800.0),
        }
    }

    #[test]
    fn second_screen_url_appends_the_path() {
        for origin in ["http://127.0.0.1:7700", "http://127.0.0.1:7700/"] {
            assert_eq!(
                second_screen_url(origin).expect("url").as_str(),
                "http://127.0.0.1:7700/second-screen"
            );
        }
        assert!(second_screen_url("http://127.0.0.1:0").is_err());
        assert!(second_screen_url("file:///tmp").is_err());
    }

    #[test]
    fn opening_twice_focuses_instead_of_a_second_window() {
        let app = tauri::test::mock_builder()
            .manage(page("http://127.0.0.1:7700"))
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .expect("mock app");
        assert_eq!(
            tauri::async_runtime::block_on(open_or_focus_second_screen(app.handle().clone())),
            Ok(SecondScreenOutcome::Opened)
        );
        assert_eq!(
            tauri::async_runtime::block_on(open_or_focus_second_screen(app.handle().clone())),
            Ok(SecondScreenOutcome::Focused),
            "if a second press opens a third window then broken"
        );
        let windows = app.webview_windows();
        assert_eq!(windows.len(), 1);
        assert!(windows.contains_key(SECOND_SCREEN_WINDOW_LABEL));
    }

    #[test]
    fn without_a_page_context_open_is_refused() {
        let app = tauri::test::mock_builder()
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .expect("mock app");
        assert!(tauri::async_runtime::block_on(open_or_focus_second_screen(app.handle().clone())).is_err());
        assert!(app.webview_windows().is_empty());
    }

    #[test]
    fn simultaneous_requests_open_one_window() {
        let app = tauri::test::mock_builder()
            .manage(page("http://127.0.0.1:7700"))
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .expect("mock app");
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(4));
        let requests: Vec<_> = (0..4)
            .map(|_| {
                let handle = app.handle().clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    tauri::async_runtime::block_on(open_or_focus_second_screen(handle))
                        .expect("open or focus")
                })
            })
            .collect();
        let outcomes: Vec<_> = requests.into_iter().map(|r| r.join().expect("request")).collect();
        assert_eq!(outcomes.iter().filter(|&&o| o == SecondScreenOutcome::Opened).count(), 1);
        assert_eq!(outcomes.iter().filter(|&&o| o == SecondScreenOutcome::Focused).count(), 3);
        assert_eq!(app.webview_windows().len(), 1);
    }

    #[test]
    fn control_shell_window_rejects_unknown_label_and_action() {
        let app = tauri::test::mock_builder()
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .expect("mock app");
        let handle = app.handle().clone();
        assert!(
            tauri::async_runtime::block_on(control_shell_window(
                handle.clone(),
                "open".into(),
                "booth".into()
            ))
            .unwrap_err()
            .contains("unknown window label")
        );
        assert!(
            tauri::async_runtime::block_on(control_shell_window(
                handle,
                "raise".into(),
                SECOND_SCREEN_WINDOW_LABEL.into()
            ))
            .unwrap_err()
            .contains("window action must be")
        );
    }

    #[test]
    fn query_labels_tracks_open_and_closed_second_screen() {
        let app = tauri::test::mock_builder()
            .manage(page("http://127.0.0.1:7700"))
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .expect("mock app");
        let handle = app.handle().clone();
        assert_eq!(query_shell_window_labels(handle.clone()), Vec::<String>::new());
        assert_eq!(
            tauri::async_runtime::block_on(control_shell_window(
                handle.clone(),
                "open".into(),
                SECOND_SCREEN_WINDOW_LABEL.into()
            )),
            Ok(())
        );
        assert_eq!(
            query_shell_window_labels(handle.clone()),
            vec![SECOND_SCREEN_WINDOW_LABEL.to_string()]
        );
        assert_eq!(
            tauri::async_runtime::block_on(control_shell_window(
                handle.clone(),
                "close".into(),
                SECOND_SCREEN_WINDOW_LABEL.into()
            )),
            Ok(())
        );
        assert_eq!(query_shell_window_labels(handle), Vec::<String>::new());
    }

    #[test]
    fn origin_change_keeps_a_closed_library_closed() {
        let app = tauri::test::mock_builder()
            .manage(page("http://127.0.0.1:7700"))
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .expect("mock app");
        assert_eq!(
            tauri::async_runtime::block_on(update_second_screen(
                app.handle().clone(), Some("http://127.0.0.1:7701".into())
            )),
            Ok(None)
        );
        assert!(app.webview_windows().is_empty());
        assert_eq!(
            *app.state::<SecondScreenPage>().origin.lock().expect("origin"),
            "http://127.0.0.1:7701"
        );
    }
}
