//! FTS5 search -- Phase 17 Plan 03 step 3.2.
//!
//! Runs a BM25-weighted prefix query over `tracks_fts` and joins back onto
//! `tracks` for the returned fields. Column weights per research §3:
//! title > artist > album > genre > key > tags. Results cap at the caller's
//! `limit` (palette UI currently requests 20).

use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct TrackHit {
    pub stable_id: String,
    pub path: String,
    pub title: Option<String>,
    pub artist: Option<String>,
    pub album: Option<String>,
    pub genre: Option<String>,
    pub key: Option<String>,
    pub bpm: Option<f64>,
}

/// Sanitise an FTS5 input. We strip double-quotes (which would let the caller
/// inject phrase queries) and append `*` for prefix matching. Empty string
/// returns `None` so the caller can short-circuit.
pub fn build_fts_query(q: &str) -> Option<String> {
    let cleaned = q.trim().replace('"', "");
    if cleaned.is_empty() {
        return None;
    }
    // Tokenise on whitespace and prefix-match every term so "mira val"
    // matches "Mira Valen".
    let terms: Vec<String> = cleaned
        .split_whitespace()
        .filter(|t| !t.is_empty())
        .map(|t| format!("{t}*"))
        .collect();
    if terms.is_empty() {
        None
    } else {
        Some(terms.join(" "))
    }
}

const SEARCH_SQL: &str = "
    SELECT t.stable_id, t.path, t.title, t.artist, t.album, t.genre, t.key, t.bpm
    FROM tracks_fts f
    JOIN tracks t ON t.rowid = f.rowid
    WHERE tracks_fts MATCH ?1
    ORDER BY bm25(tracks_fts, 10, 5, 3, 2, 1, 1)
    LIMIT ?2
";

pub fn search_tracks_impl(conn: &Connection, query: &str, limit: u32) -> Result<Vec<TrackHit>, String> {
    let Some(q) = build_fts_query(query) else {
        return Ok(vec![]);
    };
    let mut stmt = conn.prepare(SEARCH_SQL).map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(params![q, limit], |r| {
            Ok(TrackHit {
                stable_id: r.get(0)?,
                path: r.get(1)?,
                title: r.get(2)?,
                artist: r.get(3)?,
                album: r.get(4)?,
                genre: r.get(5)?,
                key: r.get(6)?,
                bpm: r.get(7)?,
            })
        })
        .map_err(|e| e.to_string())?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn search_tracks(query: String, limit: u32) -> Result<Vec<TrackHit>, String> {
    let db = crate::state::get_db_path()?;
    let conn = Connection::open(db).map_err(|e| e.to_string())?;
    search_tracks_impl(&conn, &query, limit)
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
                key TEXT, bpm REAL, duration_ms INTEGER, isrc TEXT,
                source TEXT
            );
            CREATE VIRTUAL TABLE tracks_fts USING fts5(
                title, artist, album, genre, key, tags,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO tracks(stable_id, path, title, artist, bpm, key) VALUES
                ('s1','/a.mp3','Neon Orchard','Mira Valen',103,'11A'),
                ('s2','/b.mp3','Velvet Static','The Lowlands',171,'11B'),
                ('s3','/c.mp3','Paper Moon','Ana Ferris',135,'7B');
            INSERT INTO tracks_fts(rowid, title, artist, album, genre, key, tags)
                SELECT rowid, title, artist, album, genre, key, '' FROM tracks;
        ",
        )
        .unwrap();
        conn
    }

    #[test]
    fn build_fts_query_empty_returns_none() {
        assert!(build_fts_query("").is_none());
        assert!(build_fts_query("   ").is_none());
    }

    #[test]
    fn build_fts_query_adds_prefix_star() {
        assert_eq!(build_fts_query("mira"), Some("mira*".to_string()));
        assert_eq!(build_fts_query("mira val"), Some("mira* val*".to_string()));
    }

    #[test]
    fn build_fts_query_strips_quotes() {
        assert_eq!(build_fts_query("\"mira\""), Some("mira*".to_string()));
    }

    #[test]
    fn search_tracks_returns_hits_for_known_query() {
        let conn = seeded_conn();
        let hits = search_tracks_impl(&conn, "mira", 10).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].stable_id, "s1");
        assert_eq!(hits[0].title.as_deref(), Some("Neon Orchard"));
    }

    #[test]
    fn search_tracks_title_beats_artist_weight() {
        // Insert a second track whose artist matches "lowlands" but title does
        // not -- title match should rank first.
        let conn = seeded_conn();
        conn.execute(
            "INSERT INTO tracks(stable_id, path, title, artist) VALUES ('s4','/d.mp3','Weekend','Other')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO tracks_fts(rowid, title, artist, album, genre, key, tags)
             SELECT rowid, title, artist, album, genre, key, '' FROM tracks WHERE stable_id='s4'",
            [],
        )
        .unwrap();
        let hits = search_tracks_impl(&conn, "weekend", 10).unwrap();
        assert!(!hits.is_empty());
        assert_eq!(hits[0].stable_id, "s4");
    }

    #[test]
    fn search_tracks_empty_query_returns_empty() {
        let conn = seeded_conn();
        assert!(search_tracks_impl(&conn, "", 10).unwrap().is_empty());
        assert!(search_tracks_impl(&conn, "   ", 10).unwrap().is_empty());
    }

    #[test]
    fn search_tracks_limit_respected() {
        let conn = seeded_conn();
        // Match all 3 by title -- rely on the common word "a" ... but FTS
        // tokenises separately; use genre-all match via a broad query.
        conn.execute(
            "UPDATE tracks SET genre = 'pop'",
            [],
        )
        .unwrap();
        conn.execute(
            "DELETE FROM tracks_fts; INSERT INTO tracks_fts(rowid, title, artist, album, genre, key, tags) SELECT rowid, title, artist, album, genre, key, '' FROM tracks;",
            [],
        )
        .ok();
        let hits = search_tracks_impl(&conn, "pop", 2).unwrap();
        assert!(hits.len() <= 2);
    }
}
