//! Rekordbox drag adapter (LAUNCH-02b, Phase 18 Plan 18-02 Step 2).
//!
//! Primary: `host.start_finder_drag(file_path)`.
//! Fallback 1 (sidecar XML append): for RB 6.x, append a `<TRACK>` node to
//!   `rekordbox.xml` with the Phase 1 safety ritual from `sidecar.rs`. For
//!   RB 7.x, skip this fallback (the XML pipeline is deprecated in 7) and go
//!   straight to clipboard with a toast hinting at File > Import Collection.
//! Fallback 2 (clipboard): standard clipboard copy via inherited default.
//!
//! Bundle IDs: `["com.pioneerdj.rekordboxdj", "com.alphatheta.rekordbox"]`.
//!
//! The XML append logic inserts content before the closing `</COLLECTION>`
//! tag using literal string-find (no `quick-xml` / no DOM parser dependency).
//! We do NOT do a full DOM parse; that keeps memory + allocation bounded and
//! avoids subtle reformatting of the user's existing file.
//
// @requirement("LAUNCH-02b")

use super::{
    DragAdapter, DragError, DragEvent, DragOutcome, HostBridge, Track, Vendor,
    sidecar::SidecarWrite,
};
use std::path::PathBuf;

pub const REKORDBOX_BUNDLE_IDS: &[&str] = &[
    "com.pioneerdj.rekordboxdj",  // RB 6.x
    "com.alphatheta.rekordbox",   // RB 7.x
];

/// Major Rekordbox release; drives fallback-1 routing.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum RekordboxMajor {
    Six,
    Seven,
    Unknown,
}

/// Config-overridable paths. `Default::default()` points to the standard macOS
/// locations; tests + Phase 17 wiring pass explicit paths.
pub struct RekordboxAdapter {
    pub xml_path: Option<PathBuf>,
    pub backup_dir: PathBuf,
    pub reversal_dir: PathBuf,
    pub audit_log: Option<PathBuf>,
    pub major_override: Option<RekordboxMajor>,
}

impl Default for RekordboxAdapter {
    fn default() -> Self {
        Self {
            xml_path: None, // resolved lazily from $HOME/Music/rekordbox/rekordbox.xml
            backup_dir: PathBuf::from("data/launcher/backups"),
            reversal_dir: PathBuf::from("data/launcher/reversals"),
            audit_log: Some(PathBuf::from("data/launcher/audit.jsonl")),
            major_override: None,
        }
    }
}

impl RekordboxAdapter {
    pub fn with_paths(
        xml_path: PathBuf,
        backup_dir: PathBuf,
        reversal_dir: PathBuf,
        audit_log: Option<PathBuf>,
    ) -> Self {
        Self {
            xml_path: Some(xml_path),
            backup_dir,
            reversal_dir,
            audit_log,
            major_override: None,
        }
    }

    pub fn with_major(mut self, major: RekordboxMajor) -> Self {
        self.major_override = Some(major);
        self
    }

    /// Override just the sidecar directories + audit log. `xml_path` is left
    /// untouched (so `Default::default().with_sidecar_dirs(...)` still resolves
    /// `rekordbox.xml` lazily from `$HOME/Music/rekordbox/rekordbox.xml`).
    /// Used by Phase 17 wire-up to route backups/reversals/audit under the
    /// Tauri `app_data_dir()` instead of the CWD-relative defaults.
    pub fn with_sidecar_dirs(
        mut self,
        backup_dir: PathBuf,
        reversal_dir: PathBuf,
        audit_log: Option<PathBuf>,
    ) -> Self {
        self.backup_dir = backup_dir;
        self.reversal_dir = reversal_dir;
        self.audit_log = audit_log;
        self
    }

    fn detect_major(&self, running: &[String]) -> RekordboxMajor {
        if let Some(m) = self.major_override {
            return m;
        }
        // Heuristic: prefer the running bundle id if any; else return Unknown.
        if running.iter().any(|b| b == "com.alphatheta.rekordbox") {
            RekordboxMajor::Seven
        } else if running.iter().any(|b| b == "com.pioneerdj.rekordboxdj") {
            RekordboxMajor::Six
        } else {
            RekordboxMajor::Unknown
        }
    }

    /// Publicly-callable fallback-1: append a TRACK node via sidecar ritual.
    /// Returns Fallback1Taken on success (including the dedupe case where the
    /// track already exists, which is a no-op).
    pub fn append_xml_fallback(
        &self,
        host: &dyn HostBridge,
        track: &Track,
    ) -> Result<DragOutcome, DragError> {
        let xml_path = self
            .xml_path
            .as_deref()
            .ok_or_else(|| DragError::SidecarRefused("no rekordbox.xml path configured".into()))?;

        let running = host.running_app_bundle_ids();
        let sw = SidecarWrite {
            label: "rekordbox-xml",
            target_path: xml_path,
            backup_dir: &self.backup_dir,
            reversal_dir: &self.reversal_dir,
            audit_log: self.audit_log.as_deref(),
            forbid_if_running: REKORDBOX_BUNDLE_IDS,
            running_bundle_ids: &running,
            timestamp_override: None,
        };

        let mut already_present = false;
        let track_clone = track.clone();
        let outcome = sw.with_backup(
            |content: &mut String| {
                if content_has_location(content, &track_clone.file_path.to_string_lossy()) {
                    already_present = true;
                    return Ok(());
                }
                let new_node = build_track_node(&track_clone);
                append_before_collection_close(content, &new_node)
                    .map_err(|e| e.to_string())?;
                Ok(())
            },
            |s: &str| {
                // Cheap verifier: new content must still have both </COLLECTION>
                // and </DJ_PLAYLISTS> closing tags.
                if !s.contains("</COLLECTION>") || !s.contains("</DJ_PLAYLISTS>") {
                    return Err("structural tags missing".into());
                }
                Ok(())
            },
        )?;

        let detail = if already_present {
            "already in collection".to_string()
        } else {
            format!("appended to {}", outcome.target.display())
        };
        host.emit_event(DragEvent {
            name: "drag-completed",
            vendor: Vendor::Rekordbox,
            outcome: "fallback1".into(),
            detail: detail.clone(),
        });
        Ok(DragOutcome::Fallback1Taken { detail })
    }
}

impl DragAdapter for RekordboxAdapter {
    fn vendor(&self) -> Vendor {
        Vendor::Rekordbox
    }

    fn bundle_ids(&self) -> &'static [&'static str] {
        REKORDBOX_BUNDLE_IDS
    }

    fn start(&self, host: &dyn HostBridge, track: &Track) -> Result<DragOutcome, DragError> {
        // Primary: Finder-style drag.
        match host.start_finder_drag(&track.file_path) {
            Ok(()) => {
                host.emit_event(DragEvent {
                    name: "drag-completed",
                    vendor: Vendor::Rekordbox,
                    outcome: "started".into(),
                    detail: track.file_path.to_string_lossy().to_string(),
                });
                return Ok(DragOutcome::Started);
            }
            Err(_e) => {
                // Fall through to fallbacks.
            }
        }

        // Decide fallback-1 eligibility:
        // - We need an xml_path configured.
        // - RB must not be running (enforced by sidecar ritual too, but check
        //   first so we can emit a clear toast).
        // - RB 7.x skips sidecar XML entirely -> go straight to clipboard.
        let running = host.running_app_bundle_ids();
        let major = self.detect_major(&running);

        // F18-03: treat Unknown identically to Seven. Previously a closed
        // RB7 installation surfaced as Unknown (no running bundle id) and
        // fell through to the XML sidecar path, which is an RB6-only
        // artefact -- we have no reliable way to tell a closed RB7 apart
        // from a closed RB6 by process list alone. Until F18-01 lands a
        // proper installation probe, the safe default is clipboard.
        if major == RekordboxMajor::Seven || major == RekordboxMajor::Unknown {
            let (outcome, detail) = if major == RekordboxMajor::Seven {
                (
                    "rb7-clipboard",
                    "Rekordbox 7 detected. Path copied; drop into RB or File > Import.",
                )
            } else {
                (
                    "rb-unknown-clipboard",
                    "Rekordbox major version unknown (RB closed). Path copied; drop into RB.",
                )
            };
            host.emit_event(DragEvent {
                name: "drag-fallback",
                vendor: Vendor::Rekordbox,
                outcome: outcome.into(),
                detail: detail.into(),
            });
            return self.copy_path_fallback(host, track);
        }

        if let Some(_xml_path) = self.xml_path.as_deref() {
            match self.append_xml_fallback(host, track) {
                Ok(o) => return Ok(o),
                Err(DragError::SidecarRefused(msg)) => {
                    // App is running or mtime drifted: degrade to clipboard.
                    host.emit_event(DragEvent {
                        name: "drag-fallback",
                        vendor: Vendor::Rekordbox,
                        outcome: "refused".into(),
                        detail: msg,
                    });
                    return self.copy_path_fallback(host, track);
                }
                Err(e) => return Err(e),
            }
        }

        // No XML path configured -> clipboard.
        self.copy_path_fallback(host, track)
    }
}

// ------------------------------------------------------------------
// XML helpers (small + focused, unit-tested directly).
// ------------------------------------------------------------------

/// Build a `<TRACK>` node for an rekordbox.xml collection. Minimal attribute
/// set per Plan 18-02 Step 2.6: TrackID, Location, Name, Artist, Album, Size,
/// TotalTime, DateAdded.
pub(crate) fn build_track_node(track: &Track) -> String {
    let location = file_path_to_rb_location(&track.file_path.to_string_lossy());
    // TrackID is a stable u32 derived from path hash. Keeps runs deterministic
    // for tests without colliding with legitimate RB IDs in typical usage.
    // (Real RB never sees this ID again after the user re-imports.)
    let track_id = stable_track_id(&track.file_path.to_string_lossy());
    let size = track.size_bytes.unwrap_or(0);
    let total_time = track.total_time_secs.unwrap_or(0);
    let date_added = chrono::Utc::now().format("%Y-%m-%d").to_string();
    format!(
        "        <TRACK TrackID=\"{tid}\" Name=\"{name}\" Artist=\"{artist}\" \
Album=\"{album}\" Size=\"{size}\" TotalTime=\"{ttime}\" DateAdded=\"{date}\" \
Location=\"{loc}\"/>\n",
        tid = track_id,
        name = xml_escape(&track.title),
        artist = xml_escape(&track.artist),
        album = xml_escape(&track.album),
        size = size,
        ttime = total_time,
        date = date_added,
        loc = xml_escape(&location),
    )
}

/// Rekordbox XML Location fields are URI-encoded file URLs rooted at `file://`.
///
/// Paths with spaces, `#`, `%`, or non-ASCII characters (e.g. accented
/// filenames) must be percent-encoded or Rekordbox silently fails to find
/// the track when it re-imports the XML. We preserve `/` unescaped so the
/// URL still reads as a path, and we pass anything that already starts with
/// `file://` through verbatim so pre-encoded inputs are not double-escaped.
///
/// (adv-v2-fanout 2/3, task 6.)
pub(crate) fn file_path_to_rb_location(path: &str) -> String {
    if path.starts_with("file://") {
        return path.to_string();
    }
    let mut out = String::with_capacity(path.len() + 16);
    out.push_str("file://localhost");
    for &b in path.as_bytes() {
        if is_rb_location_unreserved(b) {
            out.push(b as char);
        } else {
            // Hex-escape the byte. Matches percent-encoding's
            // `NON_ALPHANUMERIC.remove(b'/').remove(b'-').remove(b'.').remove(b'_').remove(b'~')`
            // character set.
            out.push('%');
            out.push(UPPER_HEX[(b >> 4) as usize] as char);
            out.push(UPPER_HEX[(b & 0x0f) as usize] as char);
        }
    }
    out
}

/// Bytes that are safe to leave literal inside a Rekordbox Location URL.
/// Equivalent to RFC 3986 `unreserved` plus `/` (path separators stay raw).
#[inline]
fn is_rb_location_unreserved(b: u8) -> bool {
    matches!(
        b,
        b'A'..=b'Z'
            | b'a'..=b'z'
            | b'0'..=b'9'
            | b'/'
            | b'-'
            | b'.'
            | b'_'
            | b'~'
    )
}

const UPPER_HEX: &[u8; 16] = b"0123456789ABCDEF";

pub(crate) fn stable_track_id(path: &str) -> u32 {
    // Simple FNV-1a over bytes. Good enough for uniqueness within a single
    // launcher install; the ID is a free parameter from RB's perspective.
    let mut hash: u32 = 0x811c9dc5;
    for b in path.as_bytes() {
        hash ^= *b as u32;
        hash = hash.wrapping_mul(0x01000193);
    }
    // Avoid 0 because RB TrackID is historically positive.
    if hash == 0 { 1 } else { hash }
}

pub(crate) fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

/// Return `true` if any existing `<TRACK ... Location="<needle>" />` is in the
/// content. Used for the duplicate-noop check.
pub(crate) fn content_has_location(content: &str, file_path: &str) -> bool {
    let needle = format!(
        "Location=\"{}\"",
        xml_escape(&file_path_to_rb_location(file_path))
    );
    content.contains(&needle)
}

/// Insert `node` right before the first occurrence of `</COLLECTION>`. Returns
/// an error if the tag is absent.
pub(crate) fn append_before_collection_close(
    content: &mut String,
    node: &str,
) -> Result<(), String> {
    let idx = content
        .find("</COLLECTION>")
        .ok_or_else(|| "</COLLECTION> tag not found; not a rekordbox.xml".to_string())?;
    content.insert_str(idx, node);
    Ok(())
}

// ------------------------------------------------------------------
// Tests
// ------------------------------------------------------------------

#[cfg(test)]
mod rekordbox_tests {
    // @requirement("LAUNCH-02b")
    use super::*;
    use crate::drag::mock_host::MockHost;
    use std::fs;
    use tempfile::TempDir;

    fn minimal_xml() -> &'static str {
        concat!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n",
            "<DJ_PLAYLISTS Version=\"1.0.0\">\n",
            "  <PRODUCT Name=\"rekordbox\" Version=\"6.7.7\" Company=\"AlphaTheta\"/>\n",
            "  <COLLECTION Entries=\"0\">\n",
            "  </COLLECTION>\n",
            "  <PLAYLISTS>\n",
            "    <NODE Type=\"0\" Name=\"ROOT\" Count=\"0\"/>\n",
            "  </PLAYLISTS>\n",
            "</DJ_PLAYLISTS>\n",
        )
    }

    fn fixture_track() -> Track {
        Track {
            file_path: PathBuf::from("/Users/test/Music/Library/song.mp3"),
            title: "My Song".into(),
            artist: "Some Artist".into(),
            album: "Some Album".into(),
            vendor_ids: Default::default(),
            size_bytes: Some(4_000_000),
            total_time_secs: Some(210),
        }
    }

    fn adapter_for(
        xml_path: PathBuf,
        root: &std::path::Path,
    ) -> RekordboxAdapter {
        RekordboxAdapter::with_paths(
            xml_path,
            root.join("backups"),
            root.join("reversals"),
            Some(root.join("audit.jsonl")),
        )
    }

    #[test]
    fn bundle_ids_cover_rb6_and_rb7() {
        let a = RekordboxAdapter::default();
        assert!(a.bundle_ids().contains(&"com.pioneerdj.rekordboxdj"));
        assert!(a.bundle_ids().contains(&"com.alphatheta.rekordbox"));
    }

    #[test]
    fn build_track_node_contains_core_attrs() {
        let node = build_track_node(&fixture_track());
        for needle in &[
            "TrackID=\"",
            "Name=\"My Song\"",
            "Artist=\"Some Artist\"",
            "Album=\"Some Album\"",
            "Size=\"4000000\"",
            "TotalTime=\"210\"",
            "Location=\"file://localhost/Users/test/Music/Library/song.mp3\"",
        ] {
            assert!(node.contains(needle), "node missing {}: {}", needle, node);
        }
    }

    #[test]
    fn file_path_to_rb_location_leaves_plain_ascii_untouched() {
        // Baseline: an ASCII path with no reserved bytes round-trips as
        // `file://localhost<path>` exactly as before.
        assert_eq!(
            file_path_to_rb_location("/Users/test/Music/Library/song.mp3"),
            "file://localhost/Users/test/Music/Library/song.mp3"
        );
    }

    #[test]
    fn file_path_to_rb_location_percent_encodes_spaces() {
        // Regression for adv-v2-fanout 2/3 (task 6): spaces must become %20
        // or Rekordbox silently drops the track at import time.
        assert_eq!(
            file_path_to_rb_location("/Users/dj/Music/My Tracks/song.mp3"),
            "file://localhost/Users/dj/Music/My%20Tracks/song.mp3"
        );
    }

    #[test]
    fn file_path_to_rb_location_percent_encodes_non_ascii() {
        // Regression for adv-v2-fanout 2/3 (task 6): the i-with-diaeresis
        // (U+00EF) encodes to UTF-8 bytes 0xC3 0xAF, and each byte must be
        // hex-escaped independently as %C3%AF.
        assert_eq!(
            file_path_to_rb_location("/Users/dj/Music/Naïve.mp3"),
            "file://localhost/Users/dj/Music/Na%C3%AFve.mp3"
        );
    }

    #[test]
    fn file_path_to_rb_location_percent_encodes_hash_and_percent() {
        // `#` would otherwise be parsed as a URL fragment delimiter and `%`
        // is itself the escape introducer. Both MUST be escaped.
        assert_eq!(
            file_path_to_rb_location("/Users/dj/Music/a#b.mp3"),
            "file://localhost/Users/dj/Music/a%23b.mp3"
        );
        assert_eq!(
            file_path_to_rb_location("/Users/dj/Music/50%.mp3"),
            "file://localhost/Users/dj/Music/50%25.mp3"
        );
    }

    #[test]
    fn file_path_to_rb_location_passes_file_urls_through_unchanged() {
        // If the caller already produced a `file://` URL (perhaps already
        // percent-encoded by another pipeline), we must not double-escape it.
        let already = "file://localhost/Users/dj/Music/My%20Tracks/song.mp3";
        assert_eq!(file_path_to_rb_location(already), already);

        let host_elided = "file:///Users/dj/Music/song.mp3";
        assert_eq!(file_path_to_rb_location(host_elided), host_elided);
    }

    #[test]
    fn file_path_to_rb_location_preserves_path_separators() {
        // `/` stays literal (RFC 3986 unreserved set for Rekordbox Location)
        // so the result still reads as a path, not as `%2F`-encoded bytes.
        let encoded = file_path_to_rb_location("/Users/dj/Music/x y/z.mp3");
        assert!(encoded.contains("/Users/dj/Music/x%20y/z.mp3"));
        assert!(!encoded.contains("%2F"), "/ should not be escaped: {}", encoded);
    }

    #[test]
    fn xml_escape_handles_special_chars() {
        assert_eq!(xml_escape("a & b"), "a &amp; b");
        assert_eq!(xml_escape("<x>"), "&lt;x&gt;");
        assert_eq!(xml_escape("a\"b"), "a&quot;b");
    }

    #[test]
    fn content_has_location_detects_duplicates() {
        let content = format!(
            "<TRACK Location=\"{}\"/>",
            xml_escape(&file_path_to_rb_location(
                "/Users/test/Music/Library/song.mp3"
            ))
        );
        assert!(content_has_location(
            &content,
            "/Users/test/Music/Library/song.mp3"
        ));
        assert!(!content_has_location(
            &content,
            "/Users/test/Music/Library/other.mp3"
        ));
    }

    #[test]
    fn append_before_collection_close_inserts_node() {
        let mut content = minimal_xml().to_string();
        append_before_collection_close(&mut content, "        <TRACK/>\n").unwrap();
        let idx_insert = content.find("<TRACK/>").unwrap();
        let idx_close = content.find("</COLLECTION>").unwrap();
        assert!(idx_insert < idx_close);
    }

    #[test]
    fn append_before_collection_close_errors_when_tag_missing() {
        let mut content = "<NO_COLLECTION/>".to_string();
        let err = append_before_collection_close(&mut content, "<x/>").unwrap_err();
        assert!(err.contains("</COLLECTION>"));
    }

    #[test]
    fn append_xml_fallback_end_to_end_adds_track() {
        let tmp = TempDir::new().unwrap();
        let xml_path = tmp.path().join("rekordbox.xml");
        fs::write(&xml_path, minimal_xml()).unwrap();
        let adapter = adapter_for(xml_path.clone(), tmp.path())
            .with_major(RekordboxMajor::Six);
        let host = MockHost::new(); // no apps running

        let outcome = adapter
            .append_xml_fallback(&host, &fixture_track())
            .expect("fallback");
        match outcome {
            DragOutcome::Fallback1Taken { detail } => {
                assert!(detail.contains("appended to"));
            }
            other => panic!("expected Fallback1Taken, got {:?}", other),
        }
        let result = fs::read_to_string(&xml_path).unwrap();
        assert!(result.contains("Name=\"My Song\""));
        assert!(result.contains("</COLLECTION>"));
        // Event emitted
        assert_eq!(host.events.borrow()[0].vendor, Vendor::Rekordbox);
    }

    #[test]
    fn append_xml_fallback_dedupes_existing_location() {
        let tmp = TempDir::new().unwrap();
        let xml_path = tmp.path().join("rekordbox.xml");
        // Pre-seed with the track already present.
        let track = fixture_track();
        let seeded = minimal_xml().replace(
            "  </COLLECTION>\n",
            &format!("{}  </COLLECTION>\n", build_track_node(&track)),
        );
        fs::write(&xml_path, &seeded).unwrap();
        let original_len = seeded.len();
        let adapter = adapter_for(xml_path.clone(), tmp.path())
            .with_major(RekordboxMajor::Six);
        let host = MockHost::new();

        let outcome = adapter.append_xml_fallback(&host, &track).unwrap();
        match outcome {
            DragOutcome::Fallback1Taken { detail } => {
                assert_eq!(detail, "already in collection");
            }
            other => panic!("expected Fallback1Taken(already), got {:?}", other),
        }
        // File didn't grow.
        let after = fs::read_to_string(&xml_path).unwrap();
        assert_eq!(after.len(), original_len);
    }

    #[test]
    fn append_xml_fallback_refuses_when_rb_running() {
        let tmp = TempDir::new().unwrap();
        let xml_path = tmp.path().join("rekordbox.xml");
        fs::write(&xml_path, minimal_xml()).unwrap();
        let adapter = adapter_for(xml_path.clone(), tmp.path())
            .with_major(RekordboxMajor::Six);
        let host = MockHost::with_running(&["com.pioneerdj.rekordboxdj"]);

        let err = adapter
            .append_xml_fallback(&host, &fixture_track())
            .unwrap_err();
        match err {
            DragError::SidecarRefused(msg) => {
                assert!(msg.contains("rekordboxdj"));
            }
            other => panic!("expected SidecarRefused, got {:?}", other),
        }
        // File unchanged.
        assert_eq!(fs::read_to_string(&xml_path).unwrap(), minimal_xml());
    }

    #[test]
    fn append_xml_fallback_generates_reversal_script_and_backup() {
        let tmp = TempDir::new().unwrap();
        let xml_path = tmp.path().join("rekordbox.xml");
        fs::write(&xml_path, minimal_xml()).unwrap();
        let adapter = adapter_for(xml_path.clone(), tmp.path())
            .with_major(RekordboxMajor::Six);
        let host = MockHost::new();

        adapter
            .append_xml_fallback(&host, &fixture_track())
            .unwrap();
        let backups: Vec<_> = fs::read_dir(tmp.path().join("backups"))
            .unwrap()
            .flatten()
            .collect();
        assert_eq!(backups.len(), 1);
        let reversals: Vec<_> = fs::read_dir(tmp.path().join("reversals"))
            .unwrap()
            .flatten()
            .collect();
        assert_eq!(reversals.len(), 1);
        let reversal = fs::read_to_string(reversals[0].path()).unwrap();
        assert!(reversal.contains("#!/usr/bin/env bash"));
    }

    #[test]
    fn start_primary_drag_happy_path_returns_started() {
        let tmp = TempDir::new().unwrap();
        let xml_path = tmp.path().join("rekordbox.xml");
        fs::write(&xml_path, minimal_xml()).unwrap();
        let adapter = adapter_for(xml_path, tmp.path());
        let host = MockHost::new();
        let outcome = adapter.start(&host, &fixture_track()).unwrap();
        assert_eq!(outcome, DragOutcome::Started);
    }

    #[test]
    fn start_falls_back_to_clipboard_when_rb7_and_drag_fails() {
        let tmp = TempDir::new().unwrap();
        let xml_path = tmp.path().join("rekordbox.xml");
        fs::write(&xml_path, minimal_xml()).unwrap();
        let adapter = adapter_for(xml_path, tmp.path())
            .with_major(RekordboxMajor::Seven);
        let host = MockHost::new();
        *host.drag_error.borrow_mut() = Some("drag refused".into());

        let outcome = adapter.start(&host, &fixture_track()).unwrap();
        assert_eq!(outcome, DragOutcome::Fallback2Taken);
        // Toast event and clipboard event both emitted.
        let events = host.events.borrow();
        assert!(events.iter().any(|e| e.outcome == "rb7-clipboard"));
        assert!(events.iter().any(|e| e.outcome == "clipboard"));
        assert_eq!(host.clipboard_calls.borrow().len(), 1);
    }

    #[test]
    fn start_falls_back_to_sidecar_xml_when_rb6_and_drag_fails() {
        let tmp = TempDir::new().unwrap();
        let xml_path = tmp.path().join("rekordbox.xml");
        fs::write(&xml_path, minimal_xml()).unwrap();
        let adapter = adapter_for(xml_path.clone(), tmp.path())
            .with_major(RekordboxMajor::Six);
        let host = MockHost::new();
        *host.drag_error.borrow_mut() = Some("drag refused".into());

        let outcome = adapter.start(&host, &fixture_track()).unwrap();
        match outcome {
            DragOutcome::Fallback1Taken { .. } => {}
            other => panic!("expected Fallback1Taken, got {:?}", other),
        }
        let final_xml = fs::read_to_string(&xml_path).unwrap();
        assert!(final_xml.contains("My Song"));
    }

    #[test]
    fn start_refuses_sidecar_and_goes_clipboard_when_rb6_is_running() {
        let tmp = TempDir::new().unwrap();
        let xml_path = tmp.path().join("rekordbox.xml");
        fs::write(&xml_path, minimal_xml()).unwrap();
        let adapter = adapter_for(xml_path.clone(), tmp.path())
            .with_major(RekordboxMajor::Six);
        let host = MockHost::with_running(&["com.pioneerdj.rekordboxdj"]);
        *host.drag_error.borrow_mut() = Some("drag refused".into());

        let outcome = adapter.start(&host, &fixture_track()).unwrap();
        assert_eq!(outcome, DragOutcome::Fallback2Taken);
        // XML file was not modified.
        let final_xml = fs::read_to_string(&xml_path).unwrap();
        assert_eq!(final_xml, minimal_xml());
    }
}
