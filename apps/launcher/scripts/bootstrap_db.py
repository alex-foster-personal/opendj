"""Phase 17 launcher bootstrap: build an FTS5-indexed SQLite DB for the palette.

Preference order (matches ``apps/launcher/src-tauri/src/state.rs``):

1. If ``data/state/state.db`` (Phase 5 shared-state) exists, do nothing and
   print ``SHARED_STATE_READY``.
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
from pathlib import Path
from typing import Iterable, Iterator

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


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 17 launcher DB bootstrap")
    ap.add_argument("--force", action="store_true", help="Rebuild even if shared-state exists")
    args = ap.parse_args()

    if SHARED_DB.exists() and not args.force:
        print("SHARED_STATE_READY")
        return 0

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
