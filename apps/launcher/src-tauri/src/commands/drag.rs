//! Drag-drop command -- Phase 17 Plan 02 step 2.5 PROCEED branch.
//!
//! Wraps `tauri_plugin_drag::start_drag` with a `Files(..)` payload. Callers
//! (React `TrackRow.onMouseDown`) pass an absolute path; we validate non-empty
//! + existence before invoking the plugin to keep the error surface tight.
//!
//! Fallback branches (DRAGOUT, CLIPBOARD) live in `apps/launcher/spikes/djay-drag/FINDINGS.md`
//! and are swappable by replacing this file's body. Phase 18's `src/drag/`
//! dispatcher is a higher-level router that will eventually wrap this; see
//! `apps/launcher/src-tauri/src/drag/djay.rs` TODO(phase-17) note.

use std::path::PathBuf;

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

#[tauri::command]
pub async fn start_track_drag(
    window: tauri::WebviewWindow,
    path: String,
    #[allow(unused)] stable_id: Option<String>,
) -> Result<(), String> {
    use tauri_plugin_drag::{DragItem, Image};
    let p = validate_drag_path(&path)?;
    // Tiny grey placeholder icon so start_drag has a non-empty Image.
    // Real branded thumbnail is a Phase 17 open question (see README).
    let icon_bytes = include_bytes!("../../icons/128x128.png");
    tauri_plugin_drag::start_drag(
        &window,
        DragItem::Files(vec![p]),
        Image::Raw(icon_bytes.to_vec()),
        |_result| {
            // TODO(phase-17): on DropTargetHit, fire an event the frontend
            // listens for so it can invoke('record_drag', {stable_id}).
        },
        Default::default(),
    )
    .map_err(|e| e.to_string())
}

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
}
