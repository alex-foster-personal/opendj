//! djay Pro drag adapter (LAUNCH-02a, Phase 17 reference).
//!
//! This file is a SHIM. Phase 17 owns the authoritative djay adapter; once
//! Phase 17 ships, this file becomes the thin `DragAdapter` wrapper around
//! Phase 17's djay drag code. For now it implements the same trait shape so
//! the dispatcher in Phase 18 can route to djay alongside the other three
//! vendors without waiting on Phase 17's integration.
//
// TODO(phase-17): replace this minimal shim with the wired djay adapter once
// the Phase 17 launcher shell lands. The public API (`DjayAdapter::new`,
// bundle IDs, trait impl) should stay stable so Phase 18's dispatcher keeps
// compiling unchanged.

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
