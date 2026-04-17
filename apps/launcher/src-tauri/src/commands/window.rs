//! Window visibility toggle for the floating palette.
//!
//! Called by the hotkey handler and the tray menu. The palette is
//! `visible: false` at startup (see `tauri.conf.json`) so this is the only
//! way to make it appear after launch.

use tauri::{AppHandle, Manager, WindowEvent};

/// Toggle the `palette` window visibility and focus.
pub fn toggle_palette_visibility(app: &AppHandle) {
    let Some(w) = app.get_webview_window("palette") else {
        return;
    };
    match w.is_visible() {
        Ok(true) => {
            let _ = w.hide();
        }
        _ => {
            let _ = w.show();
            let _ = w.set_focus();
        }
    }
}

/// Install an on-blur handler that hides the palette when it loses focus.
/// Mirrors Alfred / Raycast behaviour: click away to dismiss.
pub fn install_blur_autohide(app: &AppHandle) {
    let Some(w) = app.get_webview_window("palette") else {
        return;
    };
    let w_clone = w.clone();
    w.on_window_event(move |event| {
        if let WindowEvent::Focused(false) = event {
            let _ = w_clone.hide();
        }
    });
}
