//! Serato DJ Pro drag adapter (LAUNCH-02c, Phase 18 Plan 18-01 Step 4).
//!
//! Primary: `host.start_finder_drag(file_path)` -> returns `Started`.
//! Fallback 1 (watch folder): NOT wired in Plan 18-01. `SeratoAdapter::start`
//!   returns `Started` on the primary path; only falls back if the host errors.
//! Fallback 2 (clipboard): inherited from the `DragAdapter` default impl.
//!
//! Bundle IDs: `["com.serato.dj.pro"]`.
//
// @requirement("LAUNCH-02c")  -- Serato DJ Pro drag-drop

use super::{DragAdapter, DragError, DragEvent, DragOutcome, HostBridge, Track, Vendor};

pub const SERATO_BUNDLE_IDS: &[&str] = &["com.serato.dj.pro"];

pub struct SeratoAdapter;

impl SeratoAdapter {
    pub const fn new() -> Self {
        Self
    }
}

impl DragAdapter for SeratoAdapter {
    fn vendor(&self) -> Vendor {
        Vendor::Serato
    }

    fn bundle_ids(&self) -> &'static [&'static str] {
        SERATO_BUNDLE_IDS
    }

    fn start(&self, host: &dyn HostBridge, track: &Track) -> Result<DragOutcome, DragError> {
        match host.start_finder_drag(&track.file_path) {
            Ok(()) => {
                host.emit_event(DragEvent {
                    name: "drag-completed",
                    vendor: Vendor::Serato,
                    outcome: "started".into(),
                    detail: track.file_path.to_string_lossy().to_string(),
                });
                Ok(DragOutcome::Started)
            }
            Err(_e) => {
                // Fallback 1 (watch folder) intentionally not wired in Plan 18-01.
                // TODO(phase 18.1): Serato watch-folder fallback.
                // Go straight to fallback 2 (clipboard).
                self.copy_path_fallback(host, track)
            }
        }
    }
}

#[cfg(test)]
mod serato_tests {
    // @requirement("LAUNCH-02c")
    use super::*;
    use crate::drag::mock_host::MockHost;
    use std::path::PathBuf;

    fn fixture_track() -> Track {
        Track::minimal(
            PathBuf::from("/Users/test/Music/Library/song.mp3"),
            "My Song",
            "Some Artist",
        )
    }

    #[test]
    fn bundle_ids_match_expected() {
        let adapter = SeratoAdapter::new();
        assert_eq!(adapter.bundle_ids(), &["com.serato.dj.pro"]);
        assert_eq!(adapter.vendor(), Vendor::Serato);
    }

    #[test]
    fn primary_drag_calls_host_and_emits_completed_event() {
        let host = MockHost::new();
        let adapter = SeratoAdapter::new();
        let track = fixture_track();

        let outcome = adapter.start(&host, &track).unwrap();

        assert_eq!(outcome, DragOutcome::Started);
        assert_eq!(host.drag_calls.borrow().len(), 1);
        assert_eq!(host.drag_calls.borrow()[0], track.file_path);
        let events = host.events.borrow();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].name, "drag-completed");
        assert_eq!(events[0].vendor, Vendor::Serato);
        assert_eq!(events[0].outcome, "started");
    }

    #[test]
    fn clipboard_fallback_copies_path() {
        let host = MockHost::new();
        let adapter = SeratoAdapter::new();
        let track = fixture_track();

        let outcome = adapter.copy_path_fallback(&host, &track).unwrap();

        assert_eq!(outcome, DragOutcome::Fallback2Taken);
        assert_eq!(
            host.clipboard_calls.borrow()[0],
            "/Users/test/Music/Library/song.mp3"
        );
    }

    #[test]
    fn clipboard_fallback_emits_event() {
        let host = MockHost::new();
        let adapter = SeratoAdapter::new();
        let track = fixture_track();

        adapter.copy_path_fallback(&host, &track).unwrap();

        let events = host.events.borrow();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].name, "drag-fallback");
        assert_eq!(events[0].vendor, Vendor::Serato);
        assert_eq!(events[0].outcome, "clipboard");
    }

    #[test]
    fn primary_drag_failure_falls_back_to_clipboard() {
        let host = MockHost::new();
        *host.drag_error.borrow_mut() = Some("nsdragging session refused".into());
        let adapter = SeratoAdapter::new();
        let track = fixture_track();

        let outcome = adapter.start(&host, &track).unwrap();

        assert_eq!(outcome, DragOutcome::Fallback2Taken);
        assert_eq!(host.drag_calls.borrow().len(), 0); // drag_error path short-circuits
        assert_eq!(host.clipboard_calls.borrow().len(), 1);
    }
}
