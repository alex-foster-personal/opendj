"""SQLite FTS5 search index derived from state.db (global-fts5-search node).

Constraint: never mutate state.db's own schema. The index lives in a
separate sqlite file next to the source DB (``ensure_index`` derives the
name), rebuilt lazily whenever the source file's mtime moves past what is
recorded in the index's own ``search_meta`` table -- a cheap stat + compare
on every call, and a full re-ingest pass only when the source actually
changed (or the index does not exist yet, or its schema version is stale).

Indexed columns: title, artist (joined from ``tracks.artists_json``), plus
genre / comments / tags pulled from the ``track_fields`` EAV table
(apps/shared/state/schema.py) -- whichever of those field names a given
track actually carries; EAV is sparse by design, so absent fields simply
contribute empty text. Query tokenisation mirrors the launcher's Tauri
search command (apps/launcher/src-tauri/src/commands/search.rs
build_fts_query): strip double quotes, then prefix-match every
whitespace-split term. Terms are wrapped in a quoted FTS5 phrase before the
trailing ``*`` (``"term"*``) rather than a bare ``term*`` so stray
punctuation in the input (``:``, ``-``, ``(``) cannot be parsed as FTS5
query syntax.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_SCHEMA_VERSION = 1
_EAV_FIELDS: tuple[str, ...] = ("genre", "comments", "tags")


class SearchIndexUnavailable(RuntimeError):
    """Raised when the source state.db does not exist on disk."""


def index_path_for(state_db_path: Path) -> Path:
    """Sibling index file for ``state_db_path`` (e.g. state.db -> state-fts.db)."""
    return state_db_path.with_name(f"{state_db_path.stem}-fts{state_db_path.suffix}")


def build_fts_query(q: str) -> str | None:
    """Sanitise + tokenise ``q`` into an FTS5 MATCH expression, or None if empty.

    Every whitespace-split term becomes a quoted prefix query (``"term"*``);
    terms are implicitly ANDed by FTS5 when space-joined.
    """
    cleaned = q.strip().replace('"', "")
    terms = [t for t in cleaned.split() if t]
    if not terms:
        return None
    return " ".join(f'"{t}"*' for t in terms)


def _open_index(index_db_path: Path) -> sqlite3.Connection:
    index_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(index_db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _current_meta(conn: sqlite3.Connection) -> tuple[int, float] | None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='search_meta'"
    ).fetchone()
    if row is None:
        return None
    row = conn.execute(
        "SELECT schema, source_mtime FROM search_meta WHERE id = 1"
    ).fetchone()
    return (int(row[0]), float(row[1])) if row is not None else None


def _artist_of(artists_json: str | None) -> str:
    if not artists_json:
        return ""
    try:
        decoded = json.loads(artists_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    if isinstance(decoded, list):
        return ", ".join(str(a) for a in decoded if a)
    if isinstance(decoded, str):
        return decoded
    return ""


def _eav_text(raw_value_json: str | None) -> str:
    if raw_value_json is None:
        return ""
    try:
        value = json.loads(raw_value_json)
    except (json.JSONDecodeError, TypeError):
        return str(raw_value_json)
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    if value is None:
        return ""
    return str(value)


def _rebuild(conn: sqlite3.Connection, state_db_path: Path, source_mtime: float) -> None:
    state = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True)
    state.row_factory = sqlite3.Row
    try:
        tracks = state.execute(
            "SELECT stable_id, title, artists_json FROM tracks"
        ).fetchall()
        eav: dict[str, dict[str, str]] = {}
        placeholders = ",".join("?" * len(_EAV_FIELDS))
        for row in state.execute(
            f"SELECT stable_id, field_name, value_json FROM track_fields "
            f"WHERE field_name IN ({placeholders})",
            _EAV_FIELDS,
        ):
            eav.setdefault(row["stable_id"], {})[row["field_name"]] = row["value_json"]
    finally:
        state.close()

    conn.execute("DROP TABLE IF EXISTS tracks_fts")
    conn.execute("DROP TABLE IF EXISTS search_meta")
    conn.execute(
        "CREATE VIRTUAL TABLE tracks_fts USING fts5("
        "stable_id UNINDEXED, title, artist, genre, comments, tags, "
        "tokenize='unicode61 remove_diacritics 2')"
    )
    conn.execute(
        "CREATE TABLE search_meta ("
        "id INTEGER PRIMARY KEY, schema INTEGER NOT NULL, source_mtime REAL NOT NULL"
        ")"
    )
    rows = [
        (
            r["stable_id"],
            r["title"] or "",
            _artist_of(r["artists_json"]),
            _eav_text(eav.get(r["stable_id"], {}).get("genre")),
            _eav_text(eav.get(r["stable_id"], {}).get("comments")),
            _eav_text(eav.get(r["stable_id"], {}).get("tags")),
        )
        for r in tracks
    ]
    conn.executemany(
        "INSERT INTO tracks_fts(stable_id, title, artist, genre, comments, tags) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.execute(
        "INSERT INTO search_meta(id, schema, source_mtime) VALUES (1, ?, ?)",
        (_SCHEMA_VERSION, source_mtime),
    )
    conn.commit()


def ensure_index(state_db_path: Path, index_db_path: Path | None = None) -> Path:
    """Build/refresh the FTS index for ``state_db_path``; return its path.

    Raises :class:`SearchIndexUnavailable` if ``state_db_path`` does not
    exist -- there is nothing to derive an index from.
    """
    if not state_db_path.exists():
        raise SearchIndexUnavailable(f"state.db not found at {state_db_path}")
    target = index_db_path or index_path_for(state_db_path)
    source_mtime = state_db_path.stat().st_mtime
    conn = _open_index(target)
    try:
        if _current_meta(conn) != (_SCHEMA_VERSION, source_mtime):
            _rebuild(conn, state_db_path, source_mtime)
    finally:
        conn.close()
    return target


def search(
    state_db_path: Path,
    query: str,
    *,
    limit: int,
    offset: int = 0,
    index_db_path: Path | None = None,
) -> tuple[list[tuple[str, str]], int]:
    """Return ``([(stable_id, match_context), ...], total_matches)``, bm25-ranked.

    ``match_context`` is an FTS5 ``snippet()`` excerpt of whichever indexed
    column matched. Empty/whitespace-only queries short-circuit to
    ``([], 0)`` without touching the index (empty is the real "no query"
    state, not a "match everything" wildcard).
    """
    fts_query = build_fts_query(query)
    if fts_query is None:
        return [], 0
    target = ensure_index(state_db_path, index_db_path)
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM tracks_fts WHERE tracks_fts MATCH ?",
                (fts_query,),
            ).fetchone()[0]
        )
        rows = conn.execute(
            "SELECT stable_id, "
            "snippet(tracks_fts, -1, '', '', ' ... ', 10) AS context "
            "FROM tracks_fts WHERE tracks_fts MATCH ? "
            "ORDER BY bm25(tracks_fts, 10.0, 5.0, 3.0, 2.0, 1.0) "
            "LIMIT ? OFFSET ?",
            (fts_query, limit, offset),
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1]) for r in rows], total


__all__ = [
    "SearchIndexUnavailable",
    "build_fts_query",
    "ensure_index",
    "index_path_for",
    "search",
]
