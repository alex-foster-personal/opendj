//! Global hotkey registration (D2).
//!
//! Tries `Alt+Space` first; falls back to `Ctrl+Cmd+Space` on conflict. The
//! resolved binding is persisted in `tauri-plugin-store` under key
//! `preferences.hotkey` so a future settings pane can read + re-register it.

use std::str::FromStr;

use tauri::AppHandle;
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutEvent, ShortcutState};

use super::window::toggle_palette_visibility;

const PRIMARY: &str = "Alt+Space";
const FALLBACK: &str = "Ctrl+Cmd+Space";

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
    // TODO: surface a tauri::notification::Notification "Fell back to Ctrl+Cmd+Space"
    Ok(FALLBACK.into())
}

#[cfg(test)]
mod tests {
    #[test]
    fn primary_and_fallback_strings_parse() {
        use tauri_plugin_global_shortcut::Shortcut;
        use std::str::FromStr;
        assert!(Shortcut::from_str(super::PRIMARY).is_ok());
        assert!(Shortcut::from_str(super::FALLBACK).is_ok());
    }
}
