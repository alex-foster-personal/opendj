//! SQLite connection + DB-path resolution.
//!
//! Decides whether to point at Phase 5's shared-state DB
//! (`data/state/state.db`, matching `apps/shared/paths.py` `STATE_DB`) or the
//! Phase 17 bootstrap DB (`data/launcher-bootstrap.sqlite`) built by
//! `apps/launcher/scripts/bootstrap_db.py`. Also exposes a `now_unix()` helper
//! used by the frecency command.
//!
//! This module is intentionally side-effect-free on import; call `get_db_path`
//! at query time so tests (and CI without either DB) can mock it.

use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

/// Resolve the SQLite path. Preference order (Phase 5 ready):
///
/// 1. `$HYPERK_DB_PATH` env override (tests + ad-hoc).
/// 2. Phase 5 shared-state DB (`<repo>/data/state/state.db`) if present.
///    This is the canonical source once Phase 5 has shipped; the launcher
///    reads from it directly and relies on `bootstrap_db.py` to apply the
///    launcher-scoped additive migration (tracks_fts / tracks_frecency).
/// 3. Phase 17 bootstrap DB (`<repo>/data/launcher-bootstrap.sqlite`) --
///    the legacy fallback when Phase 5 isn't available yet.
///
/// Returns `Err` if NONE of the above exist, so callers can surface an
/// actionable "run scripts/bootstrap_db.py" message to the palette.
pub fn get_db_path() -> Result<PathBuf, String> {
    if let Ok(override_) = std::env::var("HYPERK_DB_PATH") {
        return Ok(PathBuf::from(override_));
    }
    let repo = repo_root();
    // Phase 5 shared-state DB wins whenever it's present.
    let shared = repo.join("data").join("state").join("state.db");
    if shared.exists() {
        return Ok(shared);
    }
    let bootstrap = repo.join("data").join("launcher-bootstrap.sqlite");
    if bootstrap.exists() {
        return Ok(bootstrap);
    }
    Err(format!(
        "no DB at {shared:?} or {bootstrap:?}; run `python apps/launcher/scripts/bootstrap_db.py`",
    ))
}

/// Walk up from CARGO_MANIFEST_DIR (src-tauri/) to the repo root.
/// Repo root is the dir containing `apps/`.
fn repo_root() -> PathBuf {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    // manifest = <repo>/apps/launcher/src-tauri -> parents()[2] = <repo>
    manifest
        .parent()
        .and_then(|p| p.parent())
        .and_then(|p| p.parent())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

pub fn now_unix() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// Process-wide mutex used to serialise tests that mutate `HYPERK_DB_PATH`.
    ///
    /// `cargo test` runs tests in parallel threads by default. `std::env::set_var`
    /// and `std::env::remove_var` are process-global and not thread-safe: if any
    /// other test (or helper) reads `HYPERK_DB_PATH` while the override window
    /// is open, the value it observes is racy. Guarding every test that touches
    /// this env var with the same `Mutex` ensures at most one thread is inside
    /// the set/read/remove window at a time, which restores determinism without
    /// forcing `RUST_TEST_THREADS=1` for the whole crate.
    static ENV_GUARD: Mutex<()> = Mutex::new(());

    #[test]
    fn env_override_takes_precedence() {
        let _guard = ENV_GUARD.lock().unwrap_or_else(|e| e.into_inner());
        // Save any caller-provided value so we restore it (rather than unset it)
        // on scope exit, in case the surrounding process actually uses it.
        let prev = std::env::var("HYPERK_DB_PATH").ok();
        std::env::set_var("HYPERK_DB_PATH", "/tmp/override.sqlite");
        let p = get_db_path().unwrap();
        assert_eq!(p, PathBuf::from("/tmp/override.sqlite"));
        match prev {
            Some(v) => std::env::set_var("HYPERK_DB_PATH", v),
            None => std::env::remove_var("HYPERK_DB_PATH"),
        }
    }

    #[test]
    fn now_unix_is_reasonable() {
        let t = now_unix();
        // any time after 2025-01-01
        assert!(t > 1_735_689_600);
    }
}
