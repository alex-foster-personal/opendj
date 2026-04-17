//! Shared sidecar-XML safety utility (Phase 18, Plan 18-02 Step 4).
//!
//! Rekordbox and Traktor adapters both append to an XML file on disk when the
//! DJ app is closed. The ritual mirrors Phase 1's live-DB safety pattern:
//!
//! 1. Refuse if any forbidden bundle-id is currently running (the caller
//!    passes a running-bundle snapshot).
//! 2. Snapshot mtime of the target file before read.
//! 3. Read target into a String.
//! 4. Caller-provided mutator transforms the String.
//! 5. Backup target to `<backup_dir>/<label>-<ts>.bak`.
//! 6. Write mutated content atomically (temp file + rename).
//! 7. Re-read + verify the new content parses (caller supplies the verifier).
//! 8. Emit a reversal script to `<reversal_dir>/<label>-<ts>.sh` that restores
//!    the backup.
//! 9. Emit an audit JSONL line.
//!
//! If ANY step after the backup fails, restore the backup and surface the
//! error.
//!
//! Tests use a `tempfile::TempDir` so nothing touches real user data.
//
// @requirement("LAUNCH-02b", "LAUNCH-02d")

use super::DragError;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

/// A single sidecar-write session. Builder-style: caller constructs it, then
/// calls `with_backup` providing a mutator + verifier closure.
pub struct SidecarWrite<'a> {
    pub label: &'a str,
    pub target_path: &'a Path,
    pub backup_dir: &'a Path,
    pub reversal_dir: &'a Path,
    pub audit_log: Option<&'a Path>,
    pub forbid_if_running: &'a [&'a str],
    /// Running bundle-id snapshot supplied by the caller. This avoids hidden
    /// coupling to `HostBridge::running_app_bundle_ids`.
    pub running_bundle_ids: &'a [String],
    /// Deterministic timestamp string; defaults to ISO-8601 UTC via chrono
    /// when `None`. Injected for tests.
    pub timestamp_override: Option<&'a str>,
}

/// Outcome returned to the adapter. `rolled_back` is `true` when verify failed
/// and we restored the backup.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SidecarOutcome {
    pub label: String,
    pub target: PathBuf,
    pub backup: PathBuf,
    pub reversal_script: PathBuf,
    pub rolled_back: bool,
}

impl<'a> SidecarWrite<'a> {
    /// Execute the safety ritual. `mutator` transforms the in-memory string.
    /// `verifier` checks the final on-disk content parses correctly; return
    /// `Ok(())` to commit or `Err(msg)` to trigger rollback.
    pub fn with_backup<M, V>(
        &self,
        mutator: M,
        verifier: V,
    ) -> Result<SidecarOutcome, DragError>
    where
        M: FnOnce(&mut String) -> Result<(), String>,
        V: FnOnce(&str) -> Result<(), String>,
    {
        // 0. Absolute-path guard: sidecar directories MUST be absolute so that
        //    backups/reversals/audit land in a deterministic location rather
        //    than being resolved relative to the process CWD. This catches
        //    callers that skip the Phase 17 `build_dispatcher_with_absolute_paths`
        //    wire-up and would otherwise silently scatter artifacts under the
        //    launcher's current working directory.
        if !self.backup_dir.is_absolute() {
            return Err(DragError::SidecarRefused(format!(
                "sidecar backup_dir must be absolute, got {}",
                self.backup_dir.display()
            )));
        }
        if !self.reversal_dir.is_absolute() {
            return Err(DragError::SidecarRefused(format!(
                "sidecar reversal_dir must be absolute, got {}",
                self.reversal_dir.display()
            )));
        }
        if let Some(audit) = self.audit_log {
            if !audit.is_absolute() {
                return Err(DragError::SidecarRefused(format!(
                    "sidecar audit_log must be absolute, got {}",
                    audit.display()
                )));
            }
        }

        // 1. Running-app gate
        for forbidden in self.forbid_if_running {
            if self.running_bundle_ids.iter().any(|b| b == forbidden) {
                return Err(DragError::SidecarRefused(format!(
                    "{} is running; refusing to edit {}",
                    forbidden,
                    self.target_path.display()
                )));
            }
        }

        // 2. Pre-read mtime snapshot
        let pre_mtime = read_mtime(self.target_path)?;

        // 3. Read
        let mut content = fs::read_to_string(self.target_path).map_err(|e| {
            DragError::SidecarFailed(format!("read {}: {}", self.target_path.display(), e))
        })?;

        // 3b. Mtime-drift check: another process must not have touched the file
        // between our pre-read stat and the read itself. Stat again, compare.
        let post_read_mtime = read_mtime(self.target_path)?;
        if post_read_mtime != pre_mtime {
            return Err(DragError::SidecarRefused(format!(
                "{} changed on disk between stat and read",
                self.target_path.display()
            )));
        }

        // 4. Mutate
        mutator(&mut content)
            .map_err(|msg| DragError::SidecarFailed(format!("mutator: {}", msg)))?;

        // Prep dirs + paths
        fs::create_dir_all(self.backup_dir).map_err(|e| {
            DragError::SidecarFailed(format!(
                "create backup_dir {}: {}",
                self.backup_dir.display(),
                e
            ))
        })?;
        fs::create_dir_all(self.reversal_dir).map_err(|e| {
            DragError::SidecarFailed(format!(
                "create reversal_dir {}: {}",
                self.reversal_dir.display(),
                e
            ))
        })?;

        let ts = self.compute_timestamp();
        let backup_path = self
            .backup_dir
            .join(format!("{}-{}.bak", self.label, ts));
        let reversal_path = self
            .reversal_dir
            .join(format!("{}-{}.sh", self.label, ts));

        // 5. Backup (copy current file to backup_path)
        fs::copy(self.target_path, &backup_path).map_err(|e| {
            DragError::SidecarFailed(format!(
                "backup {} -> {}: {}",
                self.target_path.display(),
                backup_path.display(),
                e
            ))
        })?;

        // 6. Atomic write: temp file in the same dir, then rename.
        let parent = self.target_path.parent().unwrap_or_else(|| Path::new("."));
        let tmp_path = parent.join(format!(".{}-{}.tmp", self.label, ts));
        {
            let mut f = fs::File::create(&tmp_path).map_err(|e| {
                DragError::SidecarFailed(format!(
                    "create temp {}: {}",
                    tmp_path.display(),
                    e
                ))
            })?;
            f.write_all(content.as_bytes()).map_err(|e| {
                DragError::SidecarFailed(format!("write temp: {}", e))
            })?;
            f.sync_all().ok();
        }
        fs::rename(&tmp_path, self.target_path).map_err(|e| {
            DragError::SidecarFailed(format!(
                "rename {} -> {}: {}",
                tmp_path.display(),
                self.target_path.display(),
                e
            ))
        })?;

        // 7. Verify: re-read and run verifier. If it fails, restore backup.
        let verify_content =
            fs::read_to_string(self.target_path).map_err(|e| {
                DragError::SidecarFailed(format!("re-read for verify: {}", e))
            })?;
        if let Err(msg) = verifier(&verify_content) {
            fs::copy(&backup_path, self.target_path).map_err(|e| {
                DragError::SidecarFailed(format!("rollback copy failed: {}", e))
            })?;
            // Emit audit + reversal script anyway so the user can see what happened.
            write_reversal_script(&reversal_path, &backup_path, self.target_path)?;
            if let Some(audit) = self.audit_log {
                append_audit(audit, self.label, "rollback", &backup_path, &msg)?;
            }
            return Err(DragError::SidecarFailed(format!(
                "verify failed ({}); rolled back from {}",
                msg,
                backup_path.display()
            )));
        }

        // 8. Reversal script
        write_reversal_script(&reversal_path, &backup_path, self.target_path)?;

        // 9. Audit log
        if let Some(audit) = self.audit_log {
            append_audit(audit, self.label, "ok", &backup_path, "")?;
        }

        Ok(SidecarOutcome {
            label: self.label.to_string(),
            target: self.target_path.to_path_buf(),
            backup: backup_path,
            reversal_script: reversal_path,
            rolled_back: false,
        })
    }

    fn compute_timestamp(&self) -> String {
        if let Some(ts) = self.timestamp_override {
            return ts.to_string();
        }
        // chrono utc iso8601 compact
        chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string()
    }
}

fn read_mtime(path: &Path) -> Result<SystemTime, DragError> {
    let meta = fs::metadata(path).map_err(|e| {
        DragError::SidecarFailed(format!("stat {}: {}", path.display(), e))
    })?;
    meta.modified().map_err(|e| {
        DragError::SidecarFailed(format!("mtime {}: {}", path.display(), e))
    })
}

fn write_reversal_script(
    reversal_path: &Path,
    backup: &Path,
    target: &Path,
) -> Result<(), DragError> {
    let contents = format!(
        "#!/usr/bin/env bash\n\
         # Phase 18 sidecar-write reversal script.\n\
         # Restores the pre-edit backup over the sidecar target.\n\
         set -euo pipefail\n\
         BAK={bak:?}\n\
         TARGET={target:?}\n\
         cp -p \"$BAK\" \"$TARGET\"\n\
         echo \"restored $TARGET from $BAK\"\n",
        bak = backup,
        target = target,
    );
    fs::write(reversal_path, contents).map_err(|e| {
        DragError::SidecarFailed(format!("write reversal script: {}", e))
    })?;
    // Make executable (chmod 0o755)
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut perm = fs::metadata(reversal_path)
            .map_err(|e| {
                DragError::SidecarFailed(format!("stat reversal: {}", e))
            })?
            .permissions();
        perm.set_mode(0o755);
        fs::set_permissions(reversal_path, perm).map_err(|e| {
            DragError::SidecarFailed(format!("chmod reversal: {}", e))
        })?;
    }
    Ok(())
}

fn append_audit(
    audit: &Path,
    label: &str,
    status: &str,
    backup: &Path,
    note: &str,
) -> Result<(), DragError> {
    if let Some(parent) = audit.parent() {
        fs::create_dir_all(parent).map_err(|e| {
            DragError::SidecarFailed(format!("create audit parent: {}", e))
        })?;
    }
    let line = serde_json::json!({
        "label": label,
        "status": status,
        "backup": backup.display().to_string(),
        "note": note,
        "ts": chrono::Utc::now().to_rfc3339(),
    });
    let mut f = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(audit)
        .map_err(|e| {
            DragError::SidecarFailed(format!("open audit: {}", e))
        })?;
    writeln!(f, "{}", line).map_err(|e| {
        DragError::SidecarFailed(format!("write audit: {}", e))
    })?;
    Ok(())
}

#[cfg(test)]
mod sidecar_tests {
    // @requirement("LAUNCH-02b", "LAUNCH-02d")
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    fn setup() -> (TempDir, PathBuf, PathBuf, PathBuf, PathBuf) {
        let tmp = TempDir::new().unwrap();
        let root = tmp.path().to_path_buf();
        let target = root.join("collection.xml");
        fs::write(&target, "<ROOT>hello</ROOT>\n").unwrap();
        let backup_dir = root.join("backups");
        let reversal_dir = root.join("reversals");
        let audit = root.join("audit.jsonl");
        (tmp, target, backup_dir, reversal_dir, audit)
    }

    #[test]
    fn happy_path_appends_and_writes_reversal_script() {
        let (_tmp, target, backup_dir, reversal_dir, audit) = setup();
        let running: Vec<String> = vec![];
        let sc = SidecarWrite {
            label: "test-xml",
            target_path: &target,
            backup_dir: &backup_dir,
            reversal_dir: &reversal_dir,
            audit_log: Some(&audit),
            forbid_if_running: &["com.forbid.running"],
            running_bundle_ids: &running,
            timestamp_override: Some("20260417T000000Z"),
        };
        let outcome = sc
            .with_backup(
                |s| {
                    *s = s.replace("hello", "hello-world");
                    Ok(())
                },
                |s| {
                    if s.contains("hello-world") {
                        Ok(())
                    } else {
                        Err("missing hello-world".into())
                    }
                },
            )
            .expect("happy path");
        assert!(!outcome.rolled_back);
        let final_content = fs::read_to_string(&target).unwrap();
        assert!(final_content.contains("hello-world"));
        assert!(outcome.backup.exists());
        assert!(outcome.reversal_script.exists());
        let reversal = fs::read_to_string(&outcome.reversal_script).unwrap();
        assert!(reversal.contains("set -euo pipefail"));
        assert!(reversal.contains("cp -p"));
        // Reversal script is executable
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let m = fs::metadata(&outcome.reversal_script).unwrap();
            assert_eq!(m.permissions().mode() & 0o777, 0o755);
        }
        // Audit log written
        let audit_content = fs::read_to_string(&audit).unwrap();
        assert!(audit_content.contains("\"status\":\"ok\""));
    }

    #[test]
    fn refuses_when_forbidden_app_is_running() {
        let (_tmp, target, backup_dir, reversal_dir, audit) = setup();
        let running: Vec<String> = vec!["com.forbid.running".into()];
        let sc = SidecarWrite {
            label: "test-xml",
            target_path: &target,
            backup_dir: &backup_dir,
            reversal_dir: &reversal_dir,
            audit_log: Some(&audit),
            forbid_if_running: &["com.forbid.running"],
            running_bundle_ids: &running,
            timestamp_override: Some("20260417T000000Z"),
        };
        let err = sc
            .with_backup(|s| { *s = s.replace("hello", "nope"); Ok(()) }, |_| Ok(()))
            .unwrap_err();
        match err {
            DragError::SidecarRefused(msg) => {
                assert!(msg.contains("com.forbid.running"));
            }
            other => panic!("expected SidecarRefused, got {:?}", other),
        }
        let final_content = fs::read_to_string(&target).unwrap();
        assert_eq!(final_content, "<ROOT>hello</ROOT>\n");
    }

    #[test]
    fn rollback_on_verify_failure_restores_backup() {
        let (_tmp, target, backup_dir, reversal_dir, audit) = setup();
        let running: Vec<String> = vec![];
        let sc = SidecarWrite {
            label: "test-xml",
            target_path: &target,
            backup_dir: &backup_dir,
            reversal_dir: &reversal_dir,
            audit_log: Some(&audit),
            forbid_if_running: &[],
            running_bundle_ids: &running,
            timestamp_override: Some("20260417T000000Z"),
        };
        let err = sc
            .with_backup(
                |s| {
                    *s = "BROKEN".into();
                    Ok(())
                },
                |s| {
                    if s == "BROKEN" {
                        Err("invalid xml".into())
                    } else {
                        Ok(())
                    }
                },
            )
            .unwrap_err();
        match err {
            DragError::SidecarFailed(msg) => {
                assert!(msg.contains("verify failed"));
            }
            other => panic!("expected SidecarFailed, got {:?}", other),
        }
        // Target was rolled back to original content
        let final_content = fs::read_to_string(&target).unwrap();
        assert_eq!(final_content, "<ROOT>hello</ROOT>\n");
        // Audit log recorded rollback
        let audit_content = fs::read_to_string(&audit).unwrap();
        assert!(audit_content.contains("\"status\":\"rollback\""));
    }

    #[test]
    fn mutator_error_bubbles_up_without_touching_disk() {
        let (_tmp, target, backup_dir, reversal_dir, _audit) = setup();
        let running: Vec<String> = vec![];
        let sc = SidecarWrite {
            label: "test-xml",
            target_path: &target,
            backup_dir: &backup_dir,
            reversal_dir: &reversal_dir,
            audit_log: None,
            forbid_if_running: &[],
            running_bundle_ids: &running,
            timestamp_override: Some("20260417T000000Z"),
        };
        let err = sc
            .with_backup(|_| Err("mutator exploded".into()), |_| Ok(()))
            .unwrap_err();
        match err {
            DragError::SidecarFailed(msg) => assert!(msg.contains("mutator exploded")),
            other => panic!("expected SidecarFailed, got {:?}", other),
        }
        // Target unchanged.
        let final_content = fs::read_to_string(&target).unwrap();
        assert_eq!(final_content, "<ROOT>hello</ROOT>\n");
    }

    #[test]
    fn refuses_relative_backup_dir() {
        // Regression for adv-v2-fanout 2/3 (task 3): a caller that constructs a
        // SidecarWrite with a relative backup_dir (e.g. via RekordboxAdapter's
        // CWD-relative Default impl) must be refused BEFORE we touch disk, so
        // sidecar artifacts never land under the process CWD.
        let (_tmp, target, _backup_dir_abs, reversal_dir, audit) = setup();
        let running: Vec<String> = vec![];
        let relative_backup = PathBuf::from("data/launcher/backups");
        assert!(!relative_backup.is_absolute(), "fixture sanity check");
        let sc = SidecarWrite {
            label: "test-xml",
            target_path: &target,
            backup_dir: &relative_backup,
            reversal_dir: &reversal_dir,
            audit_log: Some(&audit),
            forbid_if_running: &[],
            running_bundle_ids: &running,
            timestamp_override: Some("20260417T000000Z"),
        };
        let err = sc
            .with_backup(|_| Ok(()), |_| Ok(()))
            .unwrap_err();
        match err {
            DragError::SidecarRefused(msg) => {
                assert!(
                    msg.contains("backup_dir must be absolute"),
                    "unexpected message: {}",
                    msg
                );
                assert!(msg.contains("data/launcher/backups"), "msg: {}", msg);
            }
            other => panic!("expected SidecarRefused, got {:?}", other),
        }
        // Target left untouched.
        let final_content = fs::read_to_string(&target).unwrap();
        assert_eq!(final_content, "<ROOT>hello</ROOT>\n");
    }

    #[test]
    fn refuses_relative_reversal_dir() {
        let (_tmp, target, backup_dir, _reversal_dir_abs, audit) = setup();
        let running: Vec<String> = vec![];
        let relative_reversal = PathBuf::from("data/launcher/reversals");
        let sc = SidecarWrite {
            label: "test-xml",
            target_path: &target,
            backup_dir: &backup_dir,
            reversal_dir: &relative_reversal,
            audit_log: Some(&audit),
            forbid_if_running: &[],
            running_bundle_ids: &running,
            timestamp_override: Some("20260417T000000Z"),
        };
        let err = sc
            .with_backup(|_| Ok(()), |_| Ok(()))
            .unwrap_err();
        match err {
            DragError::SidecarRefused(msg) => {
                assert!(
                    msg.contains("reversal_dir must be absolute"),
                    "unexpected message: {}",
                    msg
                );
            }
            other => panic!("expected SidecarRefused, got {:?}", other),
        }
    }

    #[test]
    fn refuses_relative_audit_log() {
        let (_tmp, target, backup_dir, reversal_dir, _audit_abs) = setup();
        let running: Vec<String> = vec![];
        let relative_audit = PathBuf::from("data/launcher/audit.jsonl");
        let sc = SidecarWrite {
            label: "test-xml",
            target_path: &target,
            backup_dir: &backup_dir,
            reversal_dir: &reversal_dir,
            audit_log: Some(&relative_audit),
            forbid_if_running: &[],
            running_bundle_ids: &running,
            timestamp_override: Some("20260417T000000Z"),
        };
        let err = sc
            .with_backup(|_| Ok(()), |_| Ok(()))
            .unwrap_err();
        match err {
            DragError::SidecarRefused(msg) => {
                assert!(
                    msg.contains("audit_log must be absolute"),
                    "unexpected message: {}",
                    msg
                );
            }
            other => panic!("expected SidecarRefused, got {:?}", other),
        }
    }

    #[test]
    fn reversal_script_restores_original_when_run() {
        let (_tmp, target, backup_dir, reversal_dir, _audit) = setup();
        let running: Vec<String> = vec![];
        let sc = SidecarWrite {
            label: "test-xml",
            target_path: &target,
            backup_dir: &backup_dir,
            reversal_dir: &reversal_dir,
            audit_log: None,
            forbid_if_running: &[],
            running_bundle_ids: &running,
            timestamp_override: Some("20260417T000000Z"),
        };
        let outcome = sc
            .with_backup(
                |s| {
                    *s = "<NEW>changed</NEW>\n".into();
                    Ok(())
                },
                |_| Ok(()),
            )
            .unwrap();
        assert_eq!(
            fs::read_to_string(&target).unwrap(),
            "<NEW>changed</NEW>\n"
        );
        // Simulate running the reversal script: it's bash, we just read + execute
        // via std::process::Command for test determinism.
        let status = std::process::Command::new("bash")
            .arg(&outcome.reversal_script)
            .status()
            .unwrap();
        assert!(status.success());
        let restored = fs::read_to_string(&target).unwrap();
        assert_eq!(restored, "<ROOT>hello</ROOT>\n");
    }
}
