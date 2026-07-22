"""SQLite FTS5 search index derived from state.db (global-fts5-search node).

Constraint: never mutate state.db's own schema. The index lives in a
separate sqlite file next to the source DB (``ensure_index`` derives the
name), rebuilt lazily whenever SQLite's source state changes. The freshness
token covers both ``state.db`` and its active ``state.db-wal`` sidecar, so
committed WAL writes are visible before a checkpoint updates the main file.
Every call performs only cheap file metadata reads; a full re-ingest happens
only when that source state changed (or the index does not exist yet, or its
schema version is stale).

Indexed columns: title, artist (joined from ``tracks.artists_json``), plus
genre / comments / notes / tags pulled from the ``track_fields`` EAV table
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

_SCHEMA_VERSION = 2
_EAV_FIELDS: tuple[str, ...] = ("genre", "comments", "notes", "tags")
_SOURCE_READ_ATTEMPTS = 3


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


def _source_fingerprint(state_db_path: Path) -> str:
    """Return a cheap fingerprint of the SQLite main database and live WAL.

    SQLite commits into ``-wal`` without changing the main database file until
    checkpointing. Both files therefore comprise the committed source state
    visible to a read-only SQLite connection.
    """
    return json.dumps(
        (_file_fingerprint(state_db_path), _file_fingerprint(Path(f"{state_db_path}-wal"))),
        separators=(",", ":"),
    )


def _file_fingerprint(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _current_meta(conn: sqlite3.Connection) -> tuple[int, str] | None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='search_meta'"
    ).fetchone()
    if row is None:
        return None
    row = conn.execute("SELECT schema FROM search_meta WHERE id = 1").fetchone()
    if row is None:
        return None
    schema = int(row[0])
    if schema != _SCHEMA_VERSION:
        return (schema, "")
    row = conn.execute(
        "SELECT source_fingerprint FROM search_meta WHERE id = 1"
    ).fetchone()
    return (schema, str(row[0])) if row is not None else None


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


def _read_source(
    state_db_path: Path,
) -> tuple[list[sqlite3.Row], dict[str, dict[str, str]]]:
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
    return tracks, eav


def _rebuild(conn: sqlite3.Connection, state_db_path: Path) -> None:
    for _ in range(_SOURCE_READ_ATTEMPTS):
        before = _source_fingerprint(state_db_path)
        tracks, eav = _read_source(state_db_path)
        source_fingerprint = _source_fingerprint(state_db_path)
        if source_fingerprint == before:
            break
    else:
        raise SearchIndexUnavailable(
            f"state.db changed during search index rebuild at {state_db_path}"
        )

    conn.execute("BEGIN")
    try:
        conn.execute("DROP TABLE IF EXISTS tracks_fts")
        conn.execute("DROP TABLE IF EXISTS search_meta")
        conn.execute(
            "CREATE VIRTUAL TABLE tracks_fts USING fts5("
            "stable_id UNINDEXED, title, artist, genre, comments, notes, tags, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        conn.execute(
            "CREATE TABLE search_meta ("
            "id INTEGER PRIMARY KEY, schema INTEGER NOT NULL, "
            "source_fingerprint TEXT NOT NULL"
            ")"
        )
        rows = [
            (
                r["stable_id"],
                r["title"] or "",
                _artist_of(r["artists_json"]),
                _eav_text(eav.get(r["stable_id"], {}).get("genre")),
                _eav_text(eav.get(r["stable_id"], {}).get("comments")),
                _eav_text(eav.get(r["stable_id"], {}).get("notes")),
                _eav_text(eav.get(r["stable_id"], {}).get("tags")),
            )
            for r in tracks
        ]
        conn.executemany(
            "INSERT INTO tracks_fts(stable_id, title, artist, genre, comments, notes, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute(
            "INSERT INTO search_meta(id, schema, source_fingerprint) VALUES (1, ?, ?)",
            (_SCHEMA_VERSION, source_fingerprint),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def ensure_index(state_db_path: Path, index_db_path: Path | None = None) -> Path:
    """Build/refresh the FTS index for ``state_db_path``; return its path.

    Raises :class:`SearchIndexUnavailable` if ``state_db_path`` does not
    exist -- there is nothing to derive an index from.
    """
    conn: sqlite3.Connection | None = None
    try:
        if not state_db_path.exists():
            raise SearchIndexUnavailable(f"state.db not found at {state_db_path}")
        target = index_db_path or index_path_for(state_db_path)
        source_fingerprint = _source_fingerprint(state_db_path)
        conn = _open_index(target)
        if _current_meta(conn) != (_SCHEMA_VERSION, source_fingerprint):
            _rebuild(conn, state_db_path)
    except (OSError, sqlite3.Error) as exc:
        raise SearchIndexUnavailable(
            f"search index unavailable for {state_db_path}: {exc}"
        ) from exc
    finally:
        if conn is not None:
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
            "ORDER BY bm25(tracks_fts, 10.0, 5.0, 3.0, 2.0, 1.0, 1.0, 1.0) "
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
