//! Drag-drop adapter core (Phase 18, Plan 18-01 Step 3).
//!
//! Defines the `DragAdapter` trait and shared types. Per-vendor impls live in
//! sibling modules. The dispatcher routes a single drag request to whichever
//! adapter matches the currently-running DJ app.
//!
//! `HostBridge` abstracts the Tauri + macOS side-effects (startDrag, clipboard
//! write, NSWorkspace.runningApplications, event emission) so every adapter
//! can be tested with an in-memory mock. Phase 17 will supply the real Tauri
//! implementation when it ships.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

pub mod dispatcher;
pub mod djay;
pub mod preview_icon;
pub mod rekordbox;
pub mod serato;
pub mod sidecar;
pub mod traktor;

// ------------------------------------------------------------------
// Types
// ------------------------------------------------------------------

/// Minimal track payload for a drag session. `vendor_ids` is reserved for
/// future cross-library dedupe but is unused in v1; the drag payload is the
/// literal `file_path` per the open-dj v0 strawman.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Track {
    pub file_path: PathBuf,
    pub title: String,
    pub artist: String,
    pub album: String,
    pub vendor_ids: HashMap<String, String>,
    /// Size in bytes; optional because we may not have stat'd yet. Used by
    /// the Rekordbox XML sidecar writer.
    pub size_bytes: Option<u64>,
    /// Total time in seconds; used by the Rekordbox XML sidecar writer.
    pub total_time_secs: Option<u32>,
}

impl Track {
    pub fn minimal(file_path: impl Into<PathBuf>, title: &str, artist: &str) -> Self {
        Self {
            file_path: file_path.into(),
            title: title.to_string(),
            artist: artist.to_string(),
            album: String::new(),
            vendor_ids: HashMap::new(),
            size_bytes: None,
            total_time_secs: None,
        }
    }
}

/// DJ-app vendor identity. Used for dispatch + fallback routing.
#[derive(Debug, Copy, Clone, PartialEq, Eq, Hash)]
pub enum Vendor {
    Djay,
    Rekordbox,
    Serato,
    Traktor,
}

impl Vendor {
    pub fn label(&self) -> &'static str {
        match self {
            Vendor::Djay => "djay Pro",
            Vendor::Rekordbox => "Rekordbox",
            Vendor::Serato => "Serato DJ Pro",
            Vendor::Traktor => "Traktor Pro",
        }
    }
}

/// Outcome of a drag attempt. `Started` is the happy path; `Fallback*Taken`
/// means the primary drag was skipped/failed and a fallback fired; `Unsupported`
/// means we bailed with a human-readable reason (UI shows a toast).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DragOutcome {
    /// Primary Finder-style drag session started. Destination may or may not
    /// accept it (we learn from the "did it work?" toast).
    Started,
    /// Fallback 1 fired: sidecar XML append (Rekordbox) / NML append (Traktor).
    Fallback1Taken { detail: String },
    /// Fallback 2 fired: clipboard copy + toast.
    Fallback2Taken,
    /// Adapter bailed. Reason is a short human-readable string.
    Unsupported(String),
}

/// Adapter-facing errors. Keep short + domain-specific; the caller renders a
/// UI toast from these.
#[derive(Debug, thiserror::Error)]
pub enum DragError {
    #[error("host bridge failed: {0}")]
    HostBridge(String),
    #[error("sidecar write refused: {0}")]
    SidecarRefused(String),
    #[error("sidecar write failed: {0}")]
    SidecarFailed(String),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
}

// ------------------------------------------------------------------
// HostBridge: abstraction over Tauri + macOS side-effects
// ------------------------------------------------------------------

/// Per-drag event emitted to the UI layer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DragEvent {
    pub name: &'static str,
    pub vendor: Vendor,
    pub outcome: String,
    pub detail: String,
}

/// The launcher-drag crate never talks to Tauri / macOS directly. It calls the
/// host through this trait. The real implementation (Phase 17) wires each
/// method to `tauri-plugin-drag`, `tauri-plugin-clipboard-manager`, and
/// `objc2` `NSWorkspace.runningApplications`. Tests use a `MockHost`.
pub trait HostBridge {
    /// Start a Finder-style drag session with the given file. On macOS this is
    /// `startDrag([file_path])` via `tauri-plugin-drag`.
    /// Returns `Ok(())` when the drag session started (not when the drop was
    /// accepted; macOS does not report that).
    fn start_finder_drag(&self, file_path: &Path) -> Result<(), DragError>;

    /// Copy a path string to the system clipboard.
    fn copy_to_clipboard(&self, text: &str) -> Result<(), DragError>;

    /// Return the bundle-ids of currently-running apps. Real impl calls
    /// `NSWorkspace.sharedWorkspace.runningApplications`.
    fn running_app_bundle_ids(&self) -> Vec<String>;

    /// Emit a UI event for the webview.
    fn emit_event(&self, event: DragEvent);
}

// ------------------------------------------------------------------
// DragAdapter trait
// ------------------------------------------------------------------

/// Per-vendor drag adapter. Implementations live in `rekordbox.rs`, `serato.rs`,
/// `traktor.rs` (and `djay.rs` as a Phase 17 reference, re-exported from here
/// for dispatcher routing).
pub trait DragAdapter: Send + Sync {
    fn vendor(&self) -> Vendor;

    /// Bundle identifiers this adapter claims. Used by the dispatcher to map a
    /// running app to the right adapter.
    fn bundle_ids(&self) -> &'static [&'static str];

    /// Execute the primary drag path. Adapter is responsible for choosing
    /// fallbacks per its own policy.
    fn start(&self, host: &dyn HostBridge, track: &Track) -> Result<DragOutcome, DragError>;

    /// Explicit fallback 2 (clipboard) entrypoint. Called by the webview when
    /// the user clicks "No -- copy path" on the "did it work?" toast.
    fn copy_path_fallback(
        &self,
        host: &dyn HostBridge,
        track: &Track,
    ) -> Result<DragOutcome, DragError> {
        // Default implementation -- shared by all vendors. Override if needed.
        let path_str = track.file_path.to_string_lossy().to_string();
        host.copy_to_clipboard(&path_str)?;
        host.emit_event(DragEvent {
            name: "drag-fallback",
            vendor: self.vendor(),
            outcome: "clipboard".into(),
            detail: path_str,
        });
        Ok(DragOutcome::Fallback2Taken)
    }
}

// ------------------------------------------------------------------
// Mock host for tests (also useful as a doc example for Phase 17 wiring).
// ------------------------------------------------------------------

#[cfg(test)]
pub mod mock_host {
    use super::*;
    use std::cell::RefCell;

    /// In-memory host bridge used by adapter tests. Captures every side effect
    /// so tests can assert on them.
    pub struct MockHost {
        pub drag_calls: RefCell<Vec<PathBuf>>,
        pub clipboard_calls: RefCell<Vec<String>>,
        pub events: RefCell<Vec<DragEvent>>,
        pub running_bundles: RefCell<Vec<String>>,
        /// If set, start_finder_drag returns this error instead of Ok.
        pub drag_error: RefCell<Option<String>>,
    }

    impl MockHost {
        pub fn new() -> Self {
            Self {
                drag_calls: RefCell::new(Vec::new()),
                clipboard_calls: RefCell::new(Vec::new()),
                events: RefCell::new(Vec::new()),
                running_bundles: RefCell::new(Vec::new()),
                drag_error: RefCell::new(None),
            }
        }

        pub fn with_running(bundles: &[&str]) -> Self {
            let host = Self::new();
            *host.running_bundles.borrow_mut() = bundles.iter().map(|b| b.to_string()).collect();
            host
        }
    }

    impl HostBridge for MockHost {
        fn start_finder_drag(&self, file_path: &Path) -> Result<(), DragError> {
            if let Some(err) = self.drag_error.borrow().clone() {
                return Err(DragError::HostBridge(err));
            }
            self.drag_calls.borrow_mut().push(file_path.to_path_buf());
            Ok(())
        }

        fn copy_to_clipboard(&self, text: &str) -> Result<(), DragError> {
            self.clipboard_calls.borrow_mut().push(text.to_string());
            Ok(())
        }

        fn running_app_bundle_ids(&self) -> Vec<String> {
            self.running_bundles.borrow().clone()
        }

        fn emit_event(&self, event: DragEvent) {
            self.events.borrow_mut().push(event);
        }
    }
}
