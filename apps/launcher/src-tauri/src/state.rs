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

    #[test]
    fn env_override_takes_precedence() {
        std::env::set_var("HYPERK_DB_PATH", "/tmp/override.sqlite");
        let p = get_db_path().unwrap();
        assert_eq!(p, PathBuf::from("/tmp/override.sqlite"));
        std::env::remove_var("HYPERK_DB_PATH");
    }

    #[test]
    fn now_unix_is_reasonable() {
        let t = now_unix();
        // any time after 2025-01-01
        assert!(t > 1_735_689_600);
    }
}
