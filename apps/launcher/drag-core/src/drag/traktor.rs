//! Traktor Pro drag adapter (LAUNCH-02d, Phase 18 Plan 18-02 Step 3).
//!
//! Primary: `host.start_finder_drag(file_path)`.
//! Fallback 1: append `<ENTRY>` to Traktor's `collection.nml`, guarded by the
//!   same Phase 1 safety ritual as Rekordbox. Traktor's NML is standard XML
//!   with a `<COLLECTION ...>...</COLLECTION>` node.
//! Fallback 2: clipboard (inherited default).
//!
//! Bundle IDs: `["com.nativeinstruments.traktor"]`.
//!
//! `collection.nml` is typically at
//! `~/Documents/Native Instruments/Traktor <version>/collection.nml`.
//! Phase 17's wiring will resolve the newest version directory. Tests pass the
//! exact path.
//
// @requirement("LAUNCH-02d")

use super::{
    DragAdapter, DragError, DragEvent, DragOutcome, HostBridge, Track, Vendor,
    sidecar::SidecarWrite,
};
use std::path::PathBuf;

pub const TRAKTOR_BUNDLE_IDS: &[&str] = &["com.nativeinstruments.traktor"];

pub struct TraktorAdapter {
    pub nml_path: Option<PathBuf>,
    pub backup_dir: PathBuf,
    pub reversal_dir: PathBuf,
    pub audit_log: Option<PathBuf>,
}

impl Default for TraktorAdapter {
    fn default() -> Self {
        Self {
            nml_path: None,
            backup_dir: PathBuf::from("data/launcher/backups"),
            reversal_dir: PathBuf::from("data/launcher/reversals"),
            audit_log: Some(PathBuf::from("data/launcher/audit.jsonl")),
        }
    }
}

impl TraktorAdapter {
    pub fn with_paths(
        nml_path: PathBuf,
        backup_dir: PathBuf,
        reversal_dir: PathBuf,
        audit_log: Option<PathBuf>,
    ) -> Self {
        Self {
            nml_path: Some(nml_path),
            backup_dir,
            reversal_dir,
            audit_log,
        }
    }

    /// Override just the sidecar directories + audit log. `nml_path` is left
    /// untouched (so `Default::default().with_sidecar_dirs(...)` still leaves
    /// the path resolution to Phase 17 wiring). Used by the Tauri wire-up to
    /// route backups/reversals/audit under `app_data_dir()` instead of the
    /// CWD-relative defaults.
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

    pub fn append_nml_fallback(
        &self,
        host: &dyn HostBridge,
        track: &Track,
    ) -> Result<DragOutcome, DragError> {
        let nml_path = self
            .nml_path
            .as_deref()
            .ok_or_else(|| DragError::SidecarRefused("no collection.nml path configured".into()))?;

        let running = host.running_app_bundle_ids();
        let sw = SidecarWrite {
            label: "traktor-nml",
            target_path: nml_path,
            backup_dir: &self.backup_dir,
            reversal_dir: &self.reversal_dir,
            audit_log: self.audit_log.as_deref(),
            forbid_if_running: TRAKTOR_BUNDLE_IDS,
            running_bundle_ids: &running,
            timestamp_override: None,
        };

        let mut already_present = false;
        let track_clone = track.clone();
        let outcome = sw.with_backup(
            |content: &mut String| {
                if nml_has_entry(content, &track_clone.file_path.to_string_lossy()) {
                    already_present = true;
                    return Ok(());
                }
                let new_entry = build_nml_entry(&track_clone);
                append_before_collection_close(content, &new_entry)
                    .map_err(|e| e.to_string())?;
                Ok(())
            },
            |s: &str| {
                if !s.contains("</COLLECTION>") || !s.contains("</NML>") {
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
            vendor: Vendor::Traktor,
            outcome: "fallback1".into(),
            detail: detail.clone(),
        });
        Ok(DragOutcome::Fallback1Taken { detail })
    }
}

impl DragAdapter for TraktorAdapter {
    fn vendor(&self) -> Vendor {
        Vendor::Traktor
    }

    fn bundle_ids(&self) -> &'static [&'static str] {
        TRAKTOR_BUNDLE_IDS
    }

    fn start(&self, host: &dyn HostBridge, track: &Track) -> Result<DragOutcome, DragError> {
        match host.start_finder_drag(&track.file_path) {
            Ok(()) => {
                host.emit_event(DragEvent {
                    name: "drag-completed",
                    vendor: Vendor::Traktor,
                    outcome: "started".into(),
                    detail: track.file_path.to_string_lossy().to_string(),
                });
                return Ok(DragOutcome::Started);
            }
            Err(_e) => {}
        }

        if self.nml_path.is_some() {
            match self.append_nml_fallback(host, track) {
                Ok(o) => return Ok(o),
                Err(DragError::SidecarRefused(msg)) => {
                    host.emit_event(DragEvent {
                        name: "drag-fallback",
                        vendor: Vendor::Traktor,
                        outcome: "refused".into(),
                        detail: msg,
                    });
                    return self.copy_path_fallback(host, track);
                }
                Err(e) => return Err(e),
            }
        }
        self.copy_path_fallback(host, track)
    }
}

// ------------------------------------------------------------------
// NML helpers
// ------------------------------------------------------------------

/// Build a minimal `<ENTRY>` node for Traktor's `collection.nml`. We include
/// `<LOCATION>`, `<INFO>`, and `<TEMPO>` placeholders per Plan 18-02 Step 3.
pub(crate) fn build_nml_entry(track: &Track) -> String {
    let (volume, dir, file) = split_for_nml(&track.file_path.to_string_lossy());
    format!(
        "  <ENTRY TITLE=\"{title}\" ARTIST=\"{artist}\">\n\
         \x20\x20\x20\x20<LOCATION DIR=\"{dir}\" FILE=\"{file}\" VOLUME=\"{volume}\"/>\n\
         \x20\x20\x20\x20<INFO BITRATE=\"0\" PLAYTIME=\"{playtime}\"/>\n\
         \x20\x20\x20\x20<TEMPO BPM=\"0.000000\"/>\n\
         \x20\x20</ENTRY>\n",
        title = nml_escape(&track.title),
        artist = nml_escape(&track.artist),
        dir = nml_escape(&dir),
        file = nml_escape(&file),
        volume = nml_escape(&volume),
        playtime = track.total_time_secs.unwrap_or(0),
    )
}

/// Traktor's LOCATION node stores the file as (volume, dir, file). Volume is
/// the macOS mount point label, dir is path between root and filename (using
/// Traktor's odd `/:` separator), file is the basename.
pub(crate) fn split_for_nml(path: &str) -> (String, String, String) {
    let pb = std::path::Path::new(path);
    let file = pb
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    let parent = pb
        .parent()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_default();
    // Traktor uses "/:" as path separator. Convert "/a/b/c" -> "/:a/:b/:c/:".
    let dir = parent
        .trim_start_matches('/')
        .split('/')
        .filter(|s| !s.is_empty())
        .map(|seg| format!("/:{}", seg))
        .collect::<String>()
        + "/:";
    // Volume is "Macintosh HD" for standard installs; we leave it empty and
    // let Traktor resolve on re-scan. Users on non-standard volumes override.
    let volume = String::new();
    (volume, dir, file)
}

pub(crate) fn nml_has_entry(content: &str, file_path: &str) -> bool {
    let (_, _dir, file) = split_for_nml(file_path);
    let needle = format!("FILE=\"{}\"", nml_escape(&file));
    content.contains(&needle)
}

pub(crate) fn nml_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

pub(crate) fn append_before_collection_close(
    content: &mut String,
    node: &str,
) -> Result<(), String> {
    let idx = content
        .find("</COLLECTION>")
        .ok_or_else(|| "</COLLECTION> tag not found; not a collection.nml".to_string())?;
    content.insert_str(idx, node);
    Ok(())
}

// ------------------------------------------------------------------
// Tests
// ------------------------------------------------------------------

#[cfg(test)]
mod traktor_tests {
    // @requirement("LAUNCH-02d")
    use super::*;
    use crate::drag::mock_host::MockHost;
    use std::fs;
    use tempfile::TempDir;

    fn minimal_nml() -> &'static str {
        concat!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n",
            "<NML VERSION=\"19\">\n",
            "  <COLLECTION ENTRIES=\"0\">\n",
            "  </COLLECTION>\n",
            "  <PLAYLISTS>\n",
            "  </PLAYLISTS>\n",
            "</NML>\n",
        )
    }

    fn fixture_track() -> Track {
        Track {
            file_path: PathBuf::from("/Users/test/Music/Library/song.mp3"),
            title: "My Song".into(),
            artist: "Some Artist".into(),
            album: String::new(),
            vendor_ids: Default::default(),
            size_bytes: None,
            total_time_secs: Some(210),
        }
    }

    fn adapter_for(
        nml_path: PathBuf,
        root: &std::path::Path,
    ) -> TraktorAdapter {
        TraktorAdapter::with_paths(
            nml_path,
            root.join("backups"),
            root.join("reversals"),
            Some(root.join("audit.jsonl")),
        )
    }

    #[test]
    fn bundle_ids_match_expected() {
        let a = TraktorAdapter::default();
        assert_eq!(a.bundle_ids(), &["com.nativeinstruments.traktor"]);
        assert_eq!(a.vendor(), Vendor::Traktor);
    }

    #[test]
    fn split_for_nml_splits_volume_dir_file() {
        let (_vol, dir, file) = split_for_nml("/Users/test/Music/song.mp3");
        assert_eq!(file, "song.mp3");
        assert_eq!(dir, "/:Users/:test/:Music/:");
    }

    #[test]
    fn build_nml_entry_contains_core_nodes() {
        let entry = build_nml_entry(&fixture_track());
        assert!(entry.contains("TITLE=\"My Song\""));
        assert!(entry.contains("ARTIST=\"Some Artist\""));
        assert!(entry.contains("<LOCATION "));
        assert!(entry.contains("FILE=\"song.mp3\""));
        assert!(entry.contains("PLAYTIME=\"210\""));
    }

    #[test]
    fn nml_has_entry_detects_existing_file() {
        let (_v, _d, file) = split_for_nml("/Users/test/Music/Library/song.mp3");
        let content = format!("<LOCATION FILE=\"{}\"/>", nml_escape(&file));
        assert!(nml_has_entry(&content, "/Users/test/Music/Library/song.mp3"));
        assert!(!nml_has_entry(&content, "/other/file.mp3"));
    }

    #[test]
    fn append_nml_fallback_end_to_end_adds_entry() {
        let tmp = TempDir::new().unwrap();
        let nml_path = tmp.path().join("collection.nml");
        fs::write(&nml_path, minimal_nml()).unwrap();
        let adapter = adapter_for(nml_path.clone(), tmp.path());
        let host = MockHost::new();

        let outcome = adapter
            .append_nml_fallback(&host, &fixture_track())
            .unwrap();
        match outcome {
            DragOutcome::Fallback1Taken { detail } => {
                assert!(detail.contains("appended to"));
            }
            other => panic!("expected Fallback1Taken, got {:?}", other),
        }
        let result = fs::read_to_string(&nml_path).unwrap();
        assert!(result.contains("TITLE=\"My Song\""));
        assert!(result.contains("</COLLECTION>"));
        assert_eq!(host.events.borrow()[0].vendor, Vendor::Traktor);
    }

    #[test]
    fn append_nml_fallback_dedupes_existing() {
        let tmp = TempDir::new().unwrap();
        let nml_path = tmp.path().join("collection.nml");
        let track = fixture_track();
        let seeded = minimal_nml().replace(
            "  </COLLECTION>\n",
            &format!("{}  </COLLECTION>\n", build_nml_entry(&track)),
        );
        fs::write(&nml_path, &seeded).unwrap();
        let original_len = seeded.len();
        let adapter = adapter_for(nml_path.clone(), tmp.path());
        let host = MockHost::new();

        let outcome = adapter.append_nml_fallback(&host, &track).unwrap();
        match outcome {
            DragOutcome::Fallback1Taken { detail } => {
                assert_eq!(detail, "already in collection");
            }
            other => panic!("expected already-in-collection, got {:?}", other),
        }
        let after = fs::read_to_string(&nml_path).unwrap();
        assert_eq!(after.len(), original_len);
    }

    #[test]
    fn append_nml_fallback_refuses_when_traktor_running() {
        let tmp = TempDir::new().unwrap();
        let nml_path = tmp.path().join("collection.nml");
        fs::write(&nml_path, minimal_nml()).unwrap();
        let adapter = adapter_for(nml_path.clone(), tmp.path());
        let host = MockHost::with_running(&["com.nativeinstruments.traktor"]);

        let err = adapter
            .append_nml_fallback(&host, &fixture_track())
            .unwrap_err();
        match err {
            DragError::SidecarRefused(msg) => {
                assert!(msg.contains("traktor"));
            }
            other => panic!("expected SidecarRefused, got {:?}", other),
        }
        assert_eq!(fs::read_to_string(&nml_path).unwrap(), minimal_nml());
    }

    #[test]
    fn start_primary_drag_happy_path_returns_started() {
        let tmp = TempDir::new().unwrap();
        let nml_path = tmp.path().join("collection.nml");
        fs::write(&nml_path, minimal_nml()).unwrap();
        let adapter = adapter_for(nml_path, tmp.path());
        let host = MockHost::new();
        let outcome = adapter.start(&host, &fixture_track()).unwrap();
        assert_eq!(outcome, DragOutcome::Started);
    }

    #[test]
    fn start_falls_back_to_nml_when_drag_fails() {
        let tmp = TempDir::new().unwrap();
        let nml_path = tmp.path().join("collection.nml");
        fs::write(&nml_path, minimal_nml()).unwrap();
        let adapter = adapter_for(nml_path.clone(), tmp.path());
        let host = MockHost::new();
        *host.drag_error.borrow_mut() = Some("drag refused".into());

        let outcome = adapter.start(&host, &fixture_track()).unwrap();
        match outcome {
            DragOutcome::Fallback1Taken { .. } => {}
            other => panic!("expected Fallback1Taken, got {:?}", other),
        }
        let result = fs::read_to_string(&nml_path).unwrap();
        assert!(result.contains("My Song"));
    }

    #[test]
    fn start_clipboard_when_no_nml_configured() {
        // adapter without nml_path
        let adapter = TraktorAdapter::default();
        let host = MockHost::new();
        *host.drag_error.borrow_mut() = Some("drag refused".into());
        let outcome = adapter
            .start(&host, &fixture_track())
            .unwrap();
        assert_eq!(outcome, DragOutcome::Fallback2Taken);
        assert_eq!(host.clipboard_calls.borrow().len(), 1);
    }
}
