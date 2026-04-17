//! djay Pro drag adapter (LAUNCH-02a).
//!
//! djay accepts standard Finder-style drags, so this adapter's primary path
//! is the same `HostBridge::start_finder_drag` call that Serato uses, with
//! the clipboard copy-path fallback inherited from `DragAdapter` for the
//! edge case where startDrag fails. Phase 17 shipped the production
//! `TauriHostBridge` (`apps/launcher/src-tauri/src/commands/drag.rs`) and
//! Phase 18 wired this adapter into `Dispatcher::default_set()` and the
//! `start_track_drag` Tauri command, so the dispatcher now routes djay
//! drags end-to-end without any further shimming.

use super::{DragAdapter, DragError, DragEvent, DragOutcome, HostBridge, Track, Vendor};

pub const DJAY_BUNDLE_IDS: &[&str] = &[
    "com.algoriddim.djay-pro-mac",
    "com.algoriddim.djay-pro-mac-direct",
];

pub struct DjayAdapter;

impl DjayAdapter {
    pub const fn new() -> Self {
        Self
    }
}

impl DragAdapter for DjayAdapter {
    fn vendor(&self) -> Vendor {
        Vendor::Djay
    }

    fn bundle_ids(&self) -> &'static [&'static str] {
        DJAY_BUNDLE_IDS
    }

    fn start(&self, host: &dyn HostBridge, track: &Track) -> Result<DragOutcome, DragError> {
        // Same primary path as Serato; djay accepts standard Finder drags.
        match host.start_finder_drag(&track.file_path) {
            Ok(()) => {
                host.emit_event(DragEvent {
                    name: "drag-completed",
                    vendor: Vendor::Djay,
                    outcome: "started".into(),
                    detail: track.file_path.to_string_lossy().to_string(),
                });
                Ok(DragOutcome::Started)
            }
            Err(_e) => self.copy_path_fallback(host, track),
        }
    }
}

#[cfg(test)]
mod djay_tests {
    use super::*;
    use crate::drag::mock_host::MockHost;

    #[test]
    fn djay_bundle_ids_cover_both_mas_and_direct() {
        let adapter = DjayAdapter::new();
        let ids = adapter.bundle_ids();
        assert!(ids.contains(&"com.algoriddim.djay-pro-mac"));
        assert!(ids.contains(&"com.algoriddim.djay-pro-mac-direct"));
    }

    #[test]
    fn djay_primary_drag_starts_and_emits_event() {
        let host = MockHost::new();
        let adapter = DjayAdapter::new();
        let track = Track::minimal("/tmp/song.mp3", "T", "A");
        let outcome = adapter.start(&host, &track).unwrap();
        assert_eq!(outcome, DragOutcome::Started);
        assert_eq!(host.events.borrow()[0].vendor, Vendor::Djay);
    }
}
