//! Shared desktop shell window identifiers.

/// Tauri label for the primary webview window (must match capabilities JSON).
pub const MAIN_WINDOW_LABEL: &str = "main";

/// Tauri label for the second-screen library window (must match capabilities JSON).
pub const SECOND_SCREEN_WINDOW_LABEL: &str = "second-screen";

/// The SPA route the second-screen window loads, appended to the engine origin.
pub const SECOND_SCREEN_PATH: &str = "/second-screen";
