//! Drag-preview icon renderer (Phase 18, Plan 18-01 Step 3).
//!
//! Phase 17 owns the actual preview icon (the pixel-perfect rendered NSImage
//! passed to `startDrag`). This module is a stub that exposes the API Phase 18
//! adapters need: "give me a preview hint for this vendor + track". Today it
//! returns a short descriptive string that the Phase 17 renderer can turn
//! into pixels; tomorrow it may construct a real NSImage.
//!
//! Keeping this file separate means a future Phase 18.1 can add per-vendor
//! logo badging without touching adapter logic.

use super::{Track, Vendor};

/// Hint for the preview renderer. Phase 17's renderer reads this and produces
/// a drag image. The `vendor` field lets Phase 18.1 add a small vendor logo.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreviewHint {
    pub vendor: Vendor,
    pub title: String,
    pub artist: String,
}

pub fn build_preview(vendor: Vendor, track: &Track) -> PreviewHint {
    PreviewHint {
        vendor,
        title: track.title.clone(),
        artist: track.artist.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_preview_copies_title_and_artist() {
        let track = Track::minimal("/tmp/a.mp3", "Song", "Artist");
        let hint = build_preview(Vendor::Serato, &track);
        assert_eq!(hint.vendor, Vendor::Serato);
        assert_eq!(hint.title, "Song");
        assert_eq!(hint.artist, "Artist");
    }
}
