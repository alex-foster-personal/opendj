"""Phase 17 launcher bootstrap: build an FTS5-indexed SQLite DB for the palette.

Preference order (matches ``apps/launcher/src-tauri/src/state.rs``):

1. If ``data/state/state.db`` (Phase 5 shared-state) exists:
   a. Apply the launcher-scoped additive migration -- create
      ``tracks_fts`` and ``tracks_frecency`` if the launcher's palette
      needs them and Phase 5 hasn't shipped them (Phase 5 owns the core
      schema; we only ever add, never rename/drop).
   b. Print ``SHARED_STATE_READY`` + exit 0.
2. Otherwise, build ``data/launcher-bootstrap.sqlite`` from:
   * ``apps.shared.rekordbox_db.iter_tracks(open_db())`` when Rekordbox is
     readable;
   * ``apps.shared.djay_db.iter_tracks(paths.DJAY_WORKING_DB)`` when djay is
     readable.

Each track gets a temporary ``stable_id = sha1(normcase(path))[:16]`` -- this
is a stand-in for Phase 5's canonical stable_id; once Phase 5 ships the real
mapping, the bootstrap becomes a no-op.

Usage::

    python apps/launcher/scripts/bootstrap_db.py           # build / noop
    python apps/launcher/scripts/bootstrap_db.py --force   # rebuild

Exit codes:
    0   success (prints SHARED_STATE_READY or BOOTSTRAP_OK:<count>)
    2   neither source readable; can't populate
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
SHARED_DB = DATA_DIR / "state" / "state.db"
BOOTSTRAP_DB = DATA_DIR / "launcher-bootstrap.sqlite"


def _iter_rekordbox() -> Iterator[dict]:
    try:
        from apps.shared import rekordbox_db  # type: ignore
    except Exception as e:
        print(f"[bootstrap] rekordbox reader unavailable: {e}", file=sys.stderr)
        return iter(())
    try:
        db = rekordbox_db.open_db()
    except Exception as e:
        print(f"[bootstrap] Rekordbox DB not readable: {e}", file=sys.stderr)
        return iter(())

    def _gen() -> Iterator[dict]:
        for t in rekordbox_db.iter_tracks(db):
            if not t.file_path:
                continue
            yield {
                "path": str(t.file_path),
                "title": t.title or "",
                "artist": t.artist or "",
                "album": t.album or "",
                "genre": t.genre or "",
                "key": None,
                "bpm": t.bpm,
                "duration_ms": None,
                "isrc": None,
                "source": "rekordbox",
            }
    return _gen()


def _iter_djay() -> Iterator[dict]:
    try:
        from apps.shared import djay_db, paths  # type: ignore
    except Exception as e:
        print(f"[bootstrap] djay reader unavailable: {e}", file=sys.stderr)
        return iter(())
    target: Path = paths.DJAY_WORKING_DB
    if not target.exists():
        target = paths.DJAY_LIVE_DB
    if not target.exists():
        print(f"[bootstrap] djay DB not found at {target}", file=sys.stderr)
        return iter(())

    def _gen() -> Iterator[dict]:
        for t in djay_db.iter_tracks(target):
            if not getattr(t, "path", None):
                continue
            yield {
                "path": str(t.path),
                "title": getattr(t, "title", "") or "",
                "artist": getattr(t, "artist", "") or "",
                "album": "",
                "genre": "",
                "key": None,
                "bpm": None,
                "duration_ms": int(t.duration * 1000) if getattr(t, "duration", None) else None,
                "isrc": None,
                "source": "djay",
            }
    return _gen()


def stable_id_from_path(path: str) -> str:
    """Temporary stable_id: SHA-1 prefix over the normcased path. 16 chars."""
    h = hashlib.sha1(os.path.normcase(path).encode("utf-8", "surrogatepass"))
    return h.hexdigest()[:16]


SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    stable_id    TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    genre        TEXT,
    key          TEXT,
    bpm          REAL,
    duration_ms  INTEGER,
    isrc         TEXT,
    source       TEXT
);

-- Contentless FTS5 (the `tags` column has no matching tracks column --
-- reserved for Phase 6 tag unification -- so we can't use external-content
-- mode). We populate it explicitly via INSERT INTO tracks_fts(rowid, ...).
CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
    title, artist, album, genre, key, tags,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS tracks_frecency (
    stable_id        TEXT PRIMARY KEY REFERENCES tracks(stable_id),
    plays            INTEGER DEFAULT 0,
    drags            INTEGER DEFAULT 0,
    last_played_at   INTEGER,
    last_dragged_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_frecency_drags
    ON tracks_frecency(drags DESC, last_dragged_at DESC);
"""


def build_db(dst: Path, rows: Iterable[dict]) -> int:
    """(Re)build ``dst`` with the Phase 17 schema + ``rows``. Returns count."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    conn = sqlite3.connect(dst)
    try:
        conn.executescript(SCHEMA)
        seen: set[str] = set()
        inserted = 0
        for row in rows:
            sid = stable_id_from_path(row["path"])
            if sid in seen:
                continue
            seen.add(sid)
            conn.execute(
                """INSERT INTO tracks(stable_id, path, title, artist, album, genre,
                                       key, bpm, duration_ms, isrc, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, row["path"], row["title"], row["artist"], row["album"], row["genre"],
                 row.get("key"), row.get("bpm"), row.get("duration_ms"), row.get("isrc"),
                 row.get("source")),
            )
            inserted += 1
        conn.execute(
            """INSERT INTO tracks_fts(rowid, title, artist, album, genre, key, tags)
               SELECT rowid, title, artist, album, genre, key, '' FROM tracks"""
        )
        conn.commit()
        return inserted
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') "
        "AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def apply_launcher_migration(db_path: Path) -> dict[str, bool]:
    """Launcher-scoped additive migration on Phase 5's ``state.db``.

    Idempotent. Never modifies existing Phase 5 tables -- only creates
    launcher-specific extensions (``tracks_fts`` + ``tracks_frecency``)
    if they are missing. Returns a dict flagging what we actually added
    this invocation so callers can log.
    """
    result = {
        "tracks_fts_created": False,
        "tracks_frecency_created": False,
        "tracks_fts_backfilled": 0,
    }
    if not db_path.exists():
        return result
    conn = sqlite3.connect(str(db_path))
    try:
        # tracks_fts: contentless FTS5 virtual table. Mirrors the schema
        # used by BOOTSTRAP_DB above so the palette query path can treat
        # either DB interchangeably.
        had_fts = _table_exists(conn, "tracks_fts")
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
                title, artist, album, genre, key, tags,
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
        result["tracks_fts_created"] = not had_fts

        # tracks_frecency: plain table keyed by stable_id (no FK -- Phase 5
        # owns the tracks table and CREATE TABLE does not allow REFERENCES
        # to an absent table in strict-FK mode without the parent existing
        # at statement time; we soft-link via launcher code instead).
        had_frec = _table_exists(conn, "tracks_frecency")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracks_frecency (
                stable_id        TEXT PRIMARY KEY,
                plays            INTEGER DEFAULT 0,
                drags            INTEGER DEFAULT 0,
                last_played_at   INTEGER,
                last_dragged_at  INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_frecency_drags
                ON tracks_frecency(drags DESC, last_dragged_at DESC)
            """
        )
        result["tracks_frecency_created"] = not had_frec
        conn.commit()

        # P17-01: Always backfill ``tracks_fts`` when it is empty but the
        # core ``tracks`` table has rows. Without this, a launcher that
        # finds Phase 5's ``state.db`` reports SHARED_STATE_READY while
        # every palette search returns zero results -- the FTS index was
        # created (or already existed) but never populated. Idempotent:
        # a non-empty FTS table short-circuits the backfill.
        if _table_exists(conn, "tracks"):
            fts_count = conn.execute(
                "SELECT COUNT(*) FROM tracks_fts"
            ).fetchone()[0]
            tracks_count = conn.execute(
                "SELECT COUNT(*) FROM tracks"
            ).fetchone()[0]
            if fts_count == 0 and tracks_count > 0:
                # Phase 5's ``tracks`` table only guarantees ``title`` and
                # ``album``; ``artist``, ``genre``, ``key`` arrive via the
                # EAV ``track_fields`` table or denormalised columns added
                # by later migrations. We probe the live schema and fall
                # back to '' for absent columns so the backfill works on
                # any Phase 5 / launcher-bootstrap variant.
                cols = {
                    r[1] for r in conn.execute(
                        "PRAGMA table_info(tracks)"
                    ).fetchall()
                }

                def _col_or_empty(name: str) -> str:
                    return f"COALESCE({name}, '')" if name in cols else "''"

                # v6 added ``deleted_at`` (ADR 08 point 5); a launcher
                # migration run against a pre-v6 DB has no tombstones to
                # exclude, so the filter is conditional the same way the
                # optional display columns above are.
                where_sql = " WHERE deleted_at IS NULL" if "deleted_at" in cols else ""
                conn.execute(
                    f"""INSERT INTO tracks_fts(rowid, title, artist, album,
                                               genre, key, tags)
                        SELECT rowid,
                               {_col_or_empty('title')},
                               {_col_or_empty('artist')},
                               {_col_or_empty('album')},
                               {_col_or_empty('genre')},
                               {_col_or_empty('key')},
                               ''
                          FROM tracks{where_sql}"""
                )
                conn.commit()
                result["tracks_fts_backfilled"] = tracks_count
    finally:
        conn.close()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 17 launcher DB bootstrap")
    ap.add_argument("--force", action="store_true", help="Rebuild even if shared-state exists")
    args = ap.parse_args()

    if SHARED_DB.exists() and not args.force:
        # Verify Phase 5 shipped the core tables the launcher expects. If
        # ``tracks`` is missing we fall through to the bootstrap path --
        # that way an unmigrated state.db doesn't silently break the
        # palette.
        conn = sqlite3.connect(f"file:{SHARED_DB}?mode=ro", uri=True)
        try:
            has_tracks = _table_exists(conn, "tracks")
        finally:
            conn.close()
        if has_tracks:
            added = apply_launcher_migration(SHARED_DB)
            extras = [k for k, v in added.items() if v]
            if added.get("tracks_fts_backfilled"):
                print(
                    "[bootstrap] backfilled tracks_fts with "
                    f"{added['tracks_fts_backfilled']} rows",
                    file=sys.stderr,
                )
            if extras:
                print(
                    "[bootstrap] using Phase 5 state.db; "
                    f"added launcher extensions: {', '.join(extras)}",
                    file=sys.stderr,
                )
            else:
                print(
                    "[bootstrap] using Phase 5 state.db; launcher "
                    "extensions already present",
                    file=sys.stderr,
                )
            print("SHARED_STATE_READY")
            return 0
        print(
            "[bootstrap] Phase 5 state.db exists but has no tracks table; "
            "falling back to launcher bootstrap",
            file=sys.stderr,
        )

    rows: list[dict] = []
    rows.extend(_iter_rekordbox())
    rows.extend(_iter_djay())
    if not rows:
        print("[bootstrap] no tracks found from any source; aborting", file=sys.stderr)
        return 2
    count = build_db(BOOTSTRAP_DB, rows)
    print(f"BOOTSTRAP_OK: {count} tracks indexed at {BOOTSTRAP_DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
