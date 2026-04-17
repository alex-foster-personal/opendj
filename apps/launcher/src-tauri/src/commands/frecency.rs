//! Frecency (frequency + recency) ranking -- Phase 17 Plan 03.
//!
//! `tracks_frecency` schema (Phase 17 additive to Phase 5 `tracks`):
//!
//! ```sql
//! CREATE TABLE tracks_frecency (
//!     stable_id TEXT PRIMARY KEY REFERENCES tracks(stable_id),
//!     plays INTEGER DEFAULT 0,
//!     drags INTEGER DEFAULT 0,
//!     last_played_at INTEGER,
//!     last_dragged_at INTEGER
//! );
//! ```
//!
//! Score (computed in Rust at query time, not a generated column, so we can
//! tune without schema migration):
//!
//! ```text
//! score = (plays * 2 + drags) * exp(-LAMBDA * days_since_last_event)
//! ```
//!
//! with `LAMBDA = 0.1` giving a 10-day half-life (research §3).

use rusqlite::{params, Connection};
use serde::Serialize;

use super::search::TrackHit;
use crate::state::now_unix;

const LAMBDA: f64 = 0.1_f64;
const SECONDS_PER_DAY: f64 = 86_400.0_f64;

#[derive(Debug, Clone, Serialize)]
pub struct FrecentHit {
    #[serde(flatten)]
    pub track: TrackHit,
    pub score: f64,
    pub plays: i64,
    pub drags: i64,
}

/// Decay multiplier for an event `last_ts` seconds ago relative to `now`.
pub fn decay(now: i64, last_ts: Option<i64>) -> f64 {
    let Some(ts) = last_ts else { return 1.0 };
    let dt = (now - ts).max(0) as f64 / SECONDS_PER_DAY;
    (-LAMBDA * dt).exp()
}

pub fn raw_score(plays: i64, drags: i64) -> f64 {
    (plays as f64) * 2.0 + (drags as f64)
}

/// Rank the top `limit` tracks by `raw_score * decay(last_dragged_at)`.
pub fn get_frecent_top_impl(
    conn: &Connection,
    now: i64,
    limit: u32,
) -> Result<Vec<FrecentHit>, String> {
    let sql = "
        SELECT t.stable_id, t.path, t.title, t.artist, t.album, t.genre, t.key, t.bpm,
               COALESCE(f.plays, 0), COALESCE(f.drags, 0),
               f.last_played_at, f.last_dragged_at
        FROM tracks t
        LEFT JOIN tracks_frecency f ON f.stable_id = t.stable_id
        WHERE COALESCE(f.plays, 0) + COALESCE(f.drags, 0) > 0
    ";
    let mut stmt = conn.prepare(sql).map_err(|e| e.to_string())?;
    let mut hits: Vec<FrecentHit> = stmt
        .query_map([], |r| {
            let plays: i64 = r.get(8)?;
            let drags: i64 = r.get(9)?;
            let last_played: Option<i64> = r.get(10)?;
            let last_dragged: Option<i64> = r.get(11)?;
            // P17-02: use the MOST RECENT of the two timestamps rather than
            // the first non-None one. `last_dragged.or(last_played)` would
            // pin decay to a stale drag timestamp even if the track has
            // been played very recently, distorting top-200 ranking.
            let last = match (last_played, last_dragged) {
                (Some(p), Some(d)) => Some(p.max(d)),
                (Some(p), None) => Some(p),
                (None, Some(d)) => Some(d),
                (None, None) => None,
            };
            Ok(FrecentHit {
                track: TrackHit {
                    stable_id: r.get(0)?,
                    path: r.get(1)?,
                    title: r.get(2)?,
                    artist: r.get(3)?,
                    album: r.get(4)?,
                    genre: r.get(5)?,
                    key: r.get(6)?,
                    bpm: r.get(7)?,
                },
                score: raw_score(plays, drags) * decay(now, last),
                plays,
                drags,
            })
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    hits.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
    hits.truncate(limit as usize);
    Ok(hits)
}

/// UPSERT into tracks_frecency. Increments `drags` and sets
/// `last_dragged_at = now`.
pub fn record_drag_impl(conn: &Connection, stable_id: &str, now: i64) -> Result<(), String> {
    conn.execute(
        "INSERT INTO tracks_frecency(stable_id, drags, last_dragged_at)
         VALUES (?1, 1, ?2)
         ON CONFLICT(stable_id) DO UPDATE SET
             drags = drags + 1,
             last_dragged_at = ?2",
        params![stable_id, now],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub fn get_frecent_top(limit: u32) -> Result<Vec<FrecentHit>, String> {
    let db = crate::state::get_db_path()?;
    let conn = Connection::open(db).map_err(|e| e.to_string())?;
    get_frecent_top_impl(&conn, now_unix(), limit)
}

#[tauri::command]
pub fn record_drag(stable_id: String) -> Result<(), String> {
    let db = crate::state::get_db_path()?;
    let conn = Connection::open(db).map_err(|e| e.to_string())?;
    record_drag_impl(&conn, &stable_id, now_unix())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seeded_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "
            CREATE TABLE tracks (
                stable_id TEXT PRIMARY KEY, path TEXT NOT NULL,
                title TEXT, artist TEXT, album TEXT, genre TEXT,
                key TEXT, bpm REAL
            );
            CREATE TABLE tracks_frecency (
                stable_id TEXT PRIMARY KEY,
                plays INTEGER DEFAULT 0,
                drags INTEGER DEFAULT 0,
                last_played_at INTEGER,
                last_dragged_at INTEGER
            );
            INSERT INTO tracks(stable_id, path, title, artist) VALUES
                ('s1','/a.mp3','A','X'),
                ('s2','/b.mp3','B','Y'),
                ('s3','/c.mp3','C','Z');
        ",
        )
        .unwrap();
        conn
    }

    #[test]
    fn decay_is_one_when_ts_none() {
        assert_eq!(decay(1_800_000_000, None), 1.0);
    }

    #[test]
    fn decay_is_one_when_now_equals_ts() {
        assert!((decay(1_800_000_000, Some(1_800_000_000)) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn decay_half_life_10_days() {
        let now = 1_800_000_000;
        let ten_days_ago = now - (10 * 86_400);
        let d = decay(now, Some(ten_days_ago));
        // exp(-0.1 * 10) ~= 0.3679
        assert!((d - 0.3678794).abs() < 1e-4);
    }

    #[test]
    fn raw_score_weights_plays_double() {
        assert_eq!(raw_score(0, 0), 0.0);
        assert_eq!(raw_score(1, 0), 2.0);
        assert_eq!(raw_score(0, 1), 1.0);
        assert_eq!(raw_score(3, 2), 8.0);
    }

    #[test]
    fn frecency_record_drag_increments() {
        let conn = seeded_conn();
        record_drag_impl(&conn, "s1", 1_800_000_000).unwrap();
        record_drag_impl(&conn, "s1", 1_800_000_100).unwrap();
        let (d, last): (i64, i64) = conn
            .query_row(
                "SELECT drags, last_dragged_at FROM tracks_frecency WHERE stable_id='s1'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(d, 2);
        assert_eq!(last, 1_800_000_100);
    }

    #[test]
    fn frecency_get_top_orders_by_score() {
        let conn = seeded_conn();
        let now = 1_800_000_000;
        // s1: 3 drags today; s2: 1 drag today; s3: 10 drags 30 days ago
        conn.execute(
            "INSERT INTO tracks_frecency(stable_id, drags, last_dragged_at) VALUES
             ('s1', 3, ?1), ('s2', 1, ?1), ('s3', 10, ?2)",
            params![now, now - 30 * 86_400],
        )
        .unwrap();
        let hits = get_frecent_top_impl(&conn, now, 10).unwrap();
        assert_eq!(hits.len(), 3);
        assert_eq!(hits[0].track.stable_id, "s1");
        // s3 decays to 10 * exp(-3) ~= 0.498, s2 stays at 1.0 -> s2 second
        assert_eq!(hits[1].track.stable_id, "s2");
        assert_eq!(hits[2].track.stable_id, "s3");
    }

    #[test]
    fn frecency_get_top_excludes_untouched() {
        let conn = seeded_conn();
        let hits = get_frecent_top_impl(&conn, 1_800_000_000, 10).unwrap();
        assert!(hits.is_empty(), "no drags = no hits");
    }
}
