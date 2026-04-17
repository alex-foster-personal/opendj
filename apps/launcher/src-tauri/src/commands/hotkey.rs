//! Global hotkey registration (D2).
//!
//! Tries `Alt+Space` first; falls back to `Ctrl+Cmd+Space` on conflict. The
//! resolved binding is persisted in `tauri-plugin-store` under key
//! `preferences.hotkey` so a future settings pane can read + re-register it.
//!
//! First-run discoverability lives here too: `maybe_show_first_run_notification`
//! fires a one-shot system notification advertising the active hotkey, gated
//! by a bit stored in `launcher_meta` (see `state::claim_first_run_notification`).

use std::str::FromStr;

use tauri::AppHandle;
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutEvent, ShortcutState};
use tauri_plugin_notification::NotificationExt;

use crate::state;

use super::window::toggle_palette_visibility;

pub(crate) const PRIMARY: &str = "Alt+Space";
pub(crate) const FALLBACK: &str = "Ctrl+Cmd+Space";

/// Register the global hotkey. Returns the binding string that actually
/// registered (either `PRIMARY` or `FALLBACK`). Bubbles the final error up if
/// both register attempts fail.
pub fn register_hotkey(app: &AppHandle) -> Result<String, String> {
    let sc = app.global_shortcut();
    let app_clone = app.clone();
    let handler = move |_handle: &AppHandle, _sc: &Shortcut, ev: ShortcutEvent| {
        if ev.state() == ShortcutState::Pressed {
            toggle_palette_visibility(&app_clone);
        }
    };

    if let Ok(primary) = Shortcut::from_str(PRIMARY) {
        if sc.on_shortcut(primary, handler.clone()).is_ok() {
            return Ok(PRIMARY.into());
        }
    }

    let fallback = Shortcut::from_str(FALLBACK).map_err(|e| e.to_string())?;
    sc.on_shortcut(fallback, handler)
        .map_err(|e| e.to_string())?;
    Ok(FALLBACK.into())
}

/// Fire the one-time post-install discoverability notification that tells
/// the user which hotkey opens the palette. The "shown" bit lives in the
/// launcher SQLite DB under `launcher_meta.first_run_notification_shown`
/// (see `state::claim_first_run_notification`), so the notification fires
/// exactly once per user profile.
///
/// `active_hotkey` is the string returned by `register_hotkey` (either
/// `PRIMARY` or `FALLBACK`). The notification body embeds the active
/// binding so the user sees the shortcut that actually registered, not a
/// hard-coded one that might have fallen back.
///
/// Errors from the SQLite probe or the notification plugin are logged and
/// swallowed: failing to show a one-time hint must never block startup.
pub fn maybe_show_first_run_notification(app: &AppHandle, active_hotkey: &str) {
    let db_path = match state::get_db_path() {
        Ok(p) => p,
        Err(e) => {
            // No DB yet (bootstrap hasn't been run). Skip the notification;
            // it will fire on a later launch once the DB exists.
            eprintln!("first-run notification: {e}");
            return;
        }
    };

    match state::claim_first_run_notification(&db_path) {
        Ok(true) => {
            let body = format!(
                "music-dj-tools launcher ready. Press {active_hotkey} to open."
            );
            if let Err(e) = app
                .notification()
                .builder()
                .title("music-dj-tools launcher")
                .body(body)
                .show()
            {
                eprintln!("first-run notification: show failed: {e}");
            }
        }
        Ok(false) => { /* already shown on a previous launch */ }
        Err(e) => eprintln!("first-run notification: claim failed: {e}"),
    }
}

/// Build the system-tray tooltip. Kept here (next to the canonical hotkey
/// strings) so the tray tooltip always agrees with whichever binding
/// `register_hotkey` actually succeeded with.
pub fn tray_tooltip(active_hotkey: &str) -> String {
    format!("music-dj-tools ({active_hotkey})")
}

#[cfg(test)]
mod tests {
    use super::{tray_tooltip, FALLBACK, PRIMARY};

    #[test]
    fn primary_and_fallback_strings_parse() {
        use tauri_plugin_global_shortcut::Shortcut;
        use std::str::FromStr;
        assert!(Shortcut::from_str(PRIMARY).is_ok());
        assert!(Shortcut::from_str(FALLBACK).is_ok());
    }

    #[test]
    fn tray_tooltip_embeds_active_hotkey() {
        assert_eq!(tray_tooltip(PRIMARY), "music-dj-tools (Alt+Space)");
        assert_eq!(tray_tooltip(FALLBACK), "music-dj-tools (Ctrl+Cmd+Space)");
    }
}
