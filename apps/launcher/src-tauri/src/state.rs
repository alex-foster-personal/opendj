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

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::Connection;

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

// -------------------------------------------------------- launcher_meta KV
//
// A tiny key/value table used for cross-run UI bits that are too small to
// justify a whole migration elsewhere. Right now its only entry is the
// `first_run_notification_shown` flag used by `commands::hotkey` to avoid
// re-firing the discoverability notification on every app launch.
//
// The table is created on first access (idempotent `CREATE TABLE IF NOT
// EXISTS`) so it does not need a bootstrap migration.

/// Key used by `commands::hotkey` to record that the post-install
/// "press Alt+Space" notification has already been shown to this user.
pub const FIRST_RUN_NOTIFICATION_KEY: &str = "first_run_notification_shown";

fn ensure_launcher_meta(conn: &Connection) -> rusqlite::Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS launcher_meta (\n\
             key   TEXT PRIMARY KEY,\n\
             value TEXT NOT NULL\n\
         );",
    )
}

/// Read a launcher_meta value by key. Returns `Ok(None)` when the row is
/// absent (including when the table has just been created).
pub fn meta_get(db_path: &Path, key: &str) -> Result<Option<String>, String> {
    let conn = Connection::open(db_path).map_err(|e| e.to_string())?;
    ensure_launcher_meta(&conn).map_err(|e| e.to_string())?;
    let mut stmt = conn
        .prepare("SELECT value FROM launcher_meta WHERE key = ?1")
        .map_err(|e| e.to_string())?;
    let mut rows = stmt.query([key]).map_err(|e| e.to_string())?;
    if let Some(row) = rows.next().map_err(|e| e.to_string())? {
        let v: String = row.get(0).map_err(|e| e.to_string())?;
        Ok(Some(v))
    } else {
        Ok(None)
    }
}

/// Upsert a launcher_meta value. Overwrites any existing row for the key.
pub fn meta_set(db_path: &Path, key: &str, value: &str) -> Result<(), String> {
    let conn = Connection::open(db_path).map_err(|e| e.to_string())?;
    ensure_launcher_meta(&conn).map_err(|e| e.to_string())?;
    conn.execute(
        "INSERT INTO launcher_meta(key, value) VALUES (?1, ?2)\n\
         ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [key, value],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

/// Convenience wrapper around `meta_get` + `meta_set` for the first-run
/// notification bit. Returns `true` exactly once per fresh DB, then `false`
/// on every subsequent call. Encapsulates the read+write so callers cannot
/// forget to persist the flag after firing the notification.
pub fn claim_first_run_notification(db_path: &Path) -> Result<bool, String> {
    let already = meta_get(db_path, FIRST_RUN_NOTIFICATION_KEY)?;
    if already.is_some() {
        return Ok(false);
    }
    meta_set(db_path, FIRST_RUN_NOTIFICATION_KEY, "1")?;
    Ok(true)
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

    #[test]
    fn test_first_run_notification_fires_once() {
        // Use a unique tempfile so parallel cargo test workers do not collide.
        let pid = std::process::id();
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.subsec_nanos())
            .unwrap_or(0);
        let db = std::env::temp_dir().join(format!("launcher-first-run-{pid}-{nanos}.sqlite"));
        // Clean slate in case a prior run crashed mid-test.
        let _ = std::fs::remove_file(&db);

        // First call on a fresh DB should return true (fire the notification).
        let fired = claim_first_run_notification(&db).expect("first claim");
        assert!(fired, "expected first call to claim the first-run slot");

        // Second call should return false (do not re-fire).
        let fired_again = claim_first_run_notification(&db).expect("second claim");
        assert!(!fired_again, "expected second call to observe the stored flag");

        // Value is persisted under the documented key.
        let stored = meta_get(&db, FIRST_RUN_NOTIFICATION_KEY).expect("meta_get");
        assert_eq!(stored.as_deref(), Some("1"));

        let _ = std::fs::remove_file(&db);
    }
}
