//! Drag-drop command -- Phase 17 shell + Phase 18 dispatcher wiring.
//!
//! The inbound `#[tauri::command]` (`start_track_drag`) constructs a
//! `TauriHostBridge` that implements `launcher_drag_core::HostBridge`, then
//! hands a minimal `Track` payload off to
//! `Dispatcher::default_set().dispatch(&host, &track, override_vendor)`.
//! The dispatcher routes to the right per-vendor `DragAdapter` based on
//! which DJ app is currently running (via NSWorkspace.runningApplications).
//!
//! `validate_drag_path` remains exported as a pure helper so the Phase 17
//! unit tests continue to exercise the path-hygiene logic without depending
//! on the Tauri runtime.

use std::path::{Path, PathBuf};

use launcher_drag_core::{
    Dispatcher, DragError, DragEvent, DragOutcome, HostBridge, Track, Vendor,
};

use serde::Deserialize;
use tauri::{AppHandle, Emitter, Manager, WebviewWindow};
use tauri_plugin_clipboard_manager::ClipboardExt;

// -------------------------------------------------------------- path helper

/// Pure helper: validate a drag-input path. Extracted so unit tests do not
/// need the Tauri runtime.
///
/// Returns `Err(msg)` for empty strings or non-existent files.
pub fn validate_drag_path(path: &str) -> Result<PathBuf, String> {
    if path.trim().is_empty() {
        return Err("path is empty".into());
    }
    let p = PathBuf::from(path);
    if !p.exists() {
        return Err(format!("path does not exist: {path}"));
    }
    Ok(p)
}

// ---------------------------------------------------------- TauriHostBridge

/// Production `HostBridge` used by the launcher binary. Wraps:
///
///  * `drag::start_drag` (the public backend behind `tauri-plugin-drag`) for
///    the Finder-style startDrag session.
///  * `tauri-plugin-clipboard-manager` for the CLIPBOARD fallback branch.
///  * `NSWorkspace.sharedWorkspace.runningApplications` (via objc2) for
///    vendor-detection on macOS. Non-macOS builds return an empty list.
///  * `app_handle.emit(name, payload)` for drag-lifecycle UI events.
pub struct TauriHostBridge {
    pub window: WebviewWindow,
    pub app: AppHandle,
}

impl TauriHostBridge {
    pub fn new(window: WebviewWindow) -> Self {
        let app = window.app_handle().clone();
        Self { window, app }
    }
}

impl HostBridge for TauriHostBridge {
    fn start_finder_drag(&self, file_path: &Path) -> Result<(), DragError> {
        // Tiny grey placeholder icon so start_drag has a non-empty Image.
        // Real branded thumbnail is a Phase 17 open question (see README).
        let icon_bytes = include_bytes!("../../icons/128x128.png");

        drag::start_drag(
            &self.window,
            drag::DragItem::Files(vec![file_path.to_path_buf()]),
            drag::Image::Raw(icon_bytes.to_vec()),
            |_result, _pos| {
                // Drop-target callback. The drag-core dispatcher already
                // emitted the "drag-started"/fallback events; the webview
                // listens for those. Per-drop confirmation is Phase 18
                // follow-up (needs HostBridge widening).
            },
            drag::Options::default(),
        )
        .map_err(|e| DragError::HostBridge(e.to_string()))
    }

    fn copy_to_clipboard(&self, text: &str) -> Result<(), DragError> {
        self.app
            .clipboard()
            .write_text(text.to_string())
            .map_err(|e| DragError::HostBridge(e.to_string()))
    }

    fn running_app_bundle_ids(&self) -> Vec<String> {
        running_app_bundle_ids_impl()
    }

    fn emit_event(&self, event: DragEvent) {
        let payload = serde_json::json!({
            "vendor": event.vendor.label(),
            "outcome": event.outcome,
            "detail": event.detail,
        });
        let _ = self.app.emit(event.name, payload);
    }
}

// --------------------------------------------- NSWorkspace bundle-id probe

#[cfg(target_os = "macos")]
fn running_app_bundle_ids_impl() -> Vec<String> {
    use objc2_app_kit::NSWorkspace;

    // SAFETY: NSWorkspace.sharedWorkspace returns a singleton owned by AppKit;
    // we only read bundle identifiers out of NSRunningApplication objects,
    // each of which the runtime retains for us.
    unsafe {
        let workspace = NSWorkspace::sharedWorkspace();
        let apps = workspace.runningApplications();
        let mut out: Vec<String> = Vec::with_capacity(apps.len());
        for i in 0..apps.len() {
            let app = apps.objectAtIndex(i);
            if let Some(bundle) = app.bundleIdentifier() {
                out.push(bundle.to_string());
            }
        }
        out
    }
}

#[cfg(not(target_os = "macos"))]
fn running_app_bundle_ids_impl() -> Vec<String> {
    // Launcher is a macOS-only product; on other hosts we return an empty
    // list so the dispatcher reports "no DJ app running". This keeps
    // `cargo test` green on Linux CI.
    Vec::new()
}

// -------------------------------------------------------- tauri command

#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum VendorOverride {
    Djay,
    Rekordbox,
    Serato,
    Traktor,
}

impl From<VendorOverride> for Vendor {
    fn from(value: VendorOverride) -> Self {
        match value {
            VendorOverride::Djay => Vendor::Djay,
            VendorOverride::Rekordbox => Vendor::Rekordbox,
            VendorOverride::Serato => Vendor::Serato,
            VendorOverride::Traktor => Vendor::Traktor,
        }
    }
}

/// Start a drag for the track at `path`. `override_vendor`, when present,
/// forces the dispatcher to use that vendor's adapter (used by the React
/// target-picker when multiple DJ apps are running).
///
/// Returns a short outcome string ("started" / "fallback1:..." / "fallback2"
/// / "unsupported:...") for the webview to toast.
#[tauri::command]
pub async fn start_track_drag(
    window: WebviewWindow,
    path: String,
    #[allow(unused)] stable_id: Option<String>,
    override_vendor: Option<VendorOverride>,
) -> Result<String, String> {
    let validated = validate_drag_path(&path)?;
    let title = validated
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_string();
    let track = Track::minimal(validated, &title, "");
    let host = TauriHostBridge::new(window);
    let override_v: Option<Vendor> = override_vendor.map(Into::into);

    match Dispatcher::default_set().dispatch(&host, &track, override_v) {
        Ok(DragOutcome::Started) => Ok("started".into()),
        Ok(DragOutcome::Fallback1Taken { detail }) => Ok(format!("fallback1:{detail}")),
        Ok(DragOutcome::Fallback2Taken) => Ok("fallback2".into()),
        Ok(DragOutcome::Unsupported(reason)) => Ok(format!("unsupported:{reason}")),
        Err(e) => Err(e.to_string()),
    }
}

// ---------------------------------------------------------------- unit tests

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn drag_rejects_empty_path() {
        assert!(validate_drag_path("").is_err());
        assert!(validate_drag_path("   ").is_err());
    }

    #[test]
    fn drag_rejects_nonexistent_path() {
        let r = validate_drag_path("/tmp/definitely-does-not-exist-hyper-k-xyz.mp3");
        assert!(r.is_err());
        assert!(r.unwrap_err().contains("does not exist"));
    }

    #[test]
    fn drag_accepts_existing_tempfile() {
        let dir = std::env::temp_dir();
        let fp = dir.join("hyper-k-launcher-test.txt");
        fs::write(&fp, b"x").unwrap();
        let r = validate_drag_path(fp.to_str().unwrap());
        let _ = fs::remove_file(&fp);
        assert!(r.is_ok());
        assert_eq!(r.unwrap(), fp);
    }

    #[test]
    fn vendor_override_maps_to_drag_core_vendor() {
        assert_eq!(Vendor::from(VendorOverride::Djay), Vendor::Djay);
        assert_eq!(Vendor::from(VendorOverride::Rekordbox), Vendor::Rekordbox);
        assert_eq!(Vendor::from(VendorOverride::Serato), Vendor::Serato);
        assert_eq!(Vendor::from(VendorOverride::Traktor), Vendor::Traktor);
    }

    #[test]
    fn vendor_override_lowercase_deserialises() {
        let v: VendorOverride = serde_json::from_str("\"serato\"").unwrap();
        assert_eq!(Vendor::from(v), Vendor::Serato);
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn running_app_bundle_ids_is_empty_off_macos() {
        assert!(running_app_bundle_ids_impl().is_empty());
    }
}
