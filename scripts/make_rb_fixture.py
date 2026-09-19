"""Build a tiny, plain-SQLite Rekordbox fixture from the live ``master.db``.

The result lives at ``tests/fixtures/rekordbox/master.plain.db`` and is
checked into the repo so tests run offline against real data shapes. The
live DB is never mutated: we always operate on a copy + use the cached
pyrekordbox key for decryption.

Pipeline::

    data/master.db.copy  (encrypted)
        │
        │  apps.shared.rekordbox_db.decrypt_to_plain() -- the one
        │  ATTACH ... KEY '' + sqlcipher_export('plain') routine, shared
        │  with `python -m apps.shared.state.cli ingest-rb`
        ▼
    tests/fixtures/rekordbox/_tmp_plain.db  (plain sqlite, full content)
        │
        │  _select_ids(): ~50 curated IDs covering edge cases
        │  _prune(): delete from djmdContent + cascading child tables
        │  VACUUM
        ▼
    tests/fixtures/rekordbox/master.plain.db  (committed — ~200KB-1MB)

Usage::

    python -m scripts.make_rb_fixture           # safe (refuses overwrite)
    python -m scripts.make_rb_fixture --force   # rebuild, overwrite existing

This script requires pyrekordbox's key-cache to already be populated (i.e.
Rekordbox must have been run at least once on this machine so
``read_rekordbox6_options`` returns ``dp``). If that precondition fails,
we stop and drop a README at ``tests/fixtures/rekordbox/README.md``
documenting the blocker.
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path

from apps.shared import paths, rekordbox_db

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
SRC_ENCRYPTED: Path = REPO_ROOT / "data" / "master.db.copy"
FIXTURE_DIR: Path = REPO_ROOT / "tests" / "fixtures" / "rekordbox"
PLAIN_TMP: Path = FIXTURE_DIR / "_tmp_plain.db"
PLAIN_OUT: Path = FIXTURE_DIR / "master.plain.db"
README_OUT: Path = FIXTURE_DIR / "README.md"
BROKEN_CSV: Path = REPO_ROOT / "data" / "reconcile" / "broken.csv"

# Target composition for the fixture. These numbers are approximate — real
# libraries may not hit every edge case; we log what we actually got.
TARGET_COMPOSITION = {
    "normal": 10,
    "broken_links": 5,
    "streaming": 5,
    "empty_folder_path": 2,
    "unicode": 2,
    "long_path": 2,
    "missing_artist": 2,
    "missing_album": 2,
    "rating_varied": 4,  # rating 0/2/4/5 — one each
    "filler": 18,
}


def _fatal(msg: str) -> int:
    print(f"[make_rb_fixture] {msg}", file=sys.stderr)
    return 1


# ------------------------------------------------------------------ selection


def _known_broken_ids(limit: int = 5) -> list[str]:
    """Pull RB IDs known to be broken pre-Phase-1 from ``broken.csv``."""
    if not BROKEN_CSV.exists():
        return []
    ids: list[str] = []
    with BROKEN_CSV.open() as fh:
        for row in csv.DictReader(fh):
            tid = row.get("id")
            if tid:
                ids.append(tid)
            if len(ids) >= limit:
                break
    return ids


def _select_ids(plain_db: Path) -> tuple[list[str], dict[str, int]]:
    """Pick ~50 IDs covering the edge-case matrix. Returns (ids, coverage).

    ``coverage`` is a dict counting how many of each bucket we actually
    retained, for the sanity-check summary.
    """
    con = sqlite3.connect(plain_db)
    try:
        cur = con.cursor()
        chosen: list[str] = []
        coverage: dict[str, int] = {k: 0 for k in TARGET_COMPOSITION}

        def _take(sql: str, params: tuple, bucket: str, n: int) -> None:
            rows = cur.execute(
                sql + f" LIMIT {n * 3}", params
            ).fetchall()  # fetch a bit more in case some collide
            for (rid,) in rows:
                rid = str(rid)
                if rid in chosen:
                    continue
                chosen.append(rid)
                coverage[bucket] += 1
                if coverage[bucket] >= n:
                    return

        # 1. Known broken IDs from the pre-Phase-1 audit (if present in DB).
        for bid in _known_broken_ids(TARGET_COMPOSITION["broken_links"]):
            r = cur.execute(
                "SELECT ID FROM djmdContent WHERE ID = ?", (bid,)
            ).fetchone()
            if r and str(r[0]) not in chosen:
                chosen.append(str(r[0]))
                coverage["broken_links"] += 1

        # 2. Streaming tracks (spotify:, tidal:, http://).
        _take(
            "SELECT ID FROM djmdContent WHERE FolderPath LIKE 'spotify:%' "
            "OR FolderPath LIKE 'tidal:%' OR FolderPath LIKE 'http%'",
            (),
            "streaming",
            TARGET_COMPOSITION["streaming"],
        )

        # 3. Empty FolderPath.
        _take(
            "SELECT ID FROM djmdContent WHERE COALESCE(FolderPath, '') = ''",
            (),
            "empty_folder_path",
            TARGET_COMPOSITION["empty_folder_path"],
        )

        # 4. Unicode in filename (rough heuristic — any non-ASCII byte).
        _take(
            "SELECT ID FROM djmdContent WHERE FolderPath GLOB '*[^ -~]*'",
            (),
            "unicode",
            TARGET_COMPOSITION["unicode"],
        )

        # 5. Very long paths (>180 chars).
        _take(
            "SELECT ID FROM djmdContent WHERE length(FolderPath) > 180",
            (),
            "long_path",
            TARGET_COMPOSITION["long_path"],
        )

        # 6. Missing Artist (ArtistID null or 0).
        _take(
            "SELECT ID FROM djmdContent WHERE ArtistID IS NULL OR ArtistID = '0'",
            (),
            "missing_artist",
            TARGET_COMPOSITION["missing_artist"],
        )

        # 7. Missing Album.
        _take(
            "SELECT ID FROM djmdContent WHERE AlbumID IS NULL OR AlbumID = '0'",
            (),
            "missing_album",
            TARGET_COMPOSITION["missing_album"],
        )

        # 8. Rating-varied. Rekordbox 6/7 stores rating on djmdContent.Rating
        # as 0..5 (NOT rating*51 — that's the serialized wire form). We try
        # each of 0/2/4/5 and skip buckets not present in this library.
        for rating in (0, 2, 4, 5):
            for (rid,) in cur.execute(
                "SELECT ID FROM djmdContent WHERE Rating = ? LIMIT 20",
                (rating,),
            ):
                if str(rid) in chosen:
                    continue
                chosen.append(str(rid))
                coverage["rating_varied"] += 1
                break

        # 9. Normal, healthy tracks — FolderPath looks like a real file path.
        _take(
            "SELECT ID FROM djmdContent WHERE FolderPath LIKE '/%' "
            "AND ArtistID IS NOT NULL AND AlbumID IS NOT NULL",
            (),
            "normal",
            TARGET_COMPOSITION["normal"],
        )

        # 10. Filler — any remaining rows to pad to ~50.
        remaining = TARGET_COMPOSITION["filler"]
        for (rid,) in cur.execute(
            "SELECT ID FROM djmdContent ORDER BY ID LIMIT 500"
        ):
            if len(chosen) >= sum(TARGET_COMPOSITION.values()):
                break
            if str(rid) not in chosen:
                chosen.append(str(rid))
                coverage["filler"] += 1
                if coverage["filler"] >= remaining:
                    break

        return chosen, coverage
    finally:
        con.close()


# ------------------------------------------------------------------ prune


def _tables_with_column(con: sqlite3.Connection, column: str) -> list[str]:
    """Return every table (excluding views) that has ``column``."""
    out: list[str] = []
    for (name,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        cols = con.execute(f'PRAGMA table_info("{name}")').fetchall()
        if any(c[1] == column for c in cols):
            out.append(name)
    return out


def _select_playlist_ids_for(
    con: sqlite3.Connection, content_ids: Iterable[str], max_playlists: int = 3
) -> list[str]:
    """Pick up to ``max_playlists`` playlists that reference any retained ID."""
    ids_placeholder = ",".join("?" for _ in content_ids)
    rows = con.execute(
        f"SELECT PlaylistID, COUNT(*) AS n FROM djmdSongPlaylist "
        f"WHERE ContentID IN ({ids_placeholder}) "
        f"GROUP BY PlaylistID ORDER BY n DESC LIMIT ?",
        (*content_ids, max_playlists),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _prune(plain_db: Path, keep_ids: list[str]) -> dict[str, int]:
    """Delete rows from ``djmdContent`` not in ``keep_ids`` + cascading tables.

    Returns a dict ``{table: rows_remaining}`` for all touched tables.
    """
    con = sqlite3.connect(plain_db)
    con.isolation_level = None  # autocommit off via BEGIN
    try:
        cur = con.cursor()
        cur.execute("PRAGMA foreign_keys = OFF")  # prune in any order
        cur.execute("BEGIN")

        # 1. Retain 2–3 playlists touching the retained tracks.
        keep_playlists = _select_playlist_ids_for(con, keep_ids, max_playlists=3)

        # Build a temp keep table so IN (...) works on 50-item lists.
        cur.execute("CREATE TEMP TABLE keep_content(id TEXT PRIMARY KEY)")
        cur.executemany("INSERT INTO keep_content VALUES (?)", [(i,) for i in keep_ids])
        cur.execute("CREATE TEMP TABLE keep_playlists(id TEXT PRIMARY KEY)")
        cur.executemany(
            "INSERT INTO keep_playlists VALUES (?)",
            [(p,) for p in keep_playlists],
        )

        # 2. Cascade-delete every table referencing ContentID.
        content_tables = _tables_with_column(con, "ContentID")
        for t in content_tables:
            cur.execute(
                f'DELETE FROM "{t}" WHERE ContentID NOT IN (SELECT id FROM keep_content)'
            )

        # 3. Delete djmdContent rows we're not keeping.
        cur.execute(
            'DELETE FROM djmdContent WHERE ID NOT IN (SELECT id FROM keep_content)'
        )

        # 4. Prune playlists: keep only those we chose + ensure djmdSongPlaylist
        #    refs are intersected with kept playlists too.
        cur.execute(
            'DELETE FROM djmdSongPlaylist WHERE PlaylistID NOT IN '
            '(SELECT id FROM keep_playlists)'
        )
        # djmdPlaylist itself: keep chosen playlists + any parent chain.
        cur.execute(
            'DELETE FROM djmdPlaylist WHERE ID NOT IN '
            '(SELECT id FROM keep_playlists)'
        )

        # 5. Orphan cleanup: for every other ContentID-less table that has
        #    a PlaylistID column, prune to kept playlists too.
        for t in _tables_with_column(con, "PlaylistID"):
            if t in ("djmdSongPlaylist", "djmdPlaylist"):
                continue
            try:
                cur.execute(
                    f'DELETE FROM "{t}" WHERE PlaylistID NOT IN '
                    f'(SELECT id FROM keep_playlists)'
                )
            except sqlite3.Error:
                pass

        # 6. Clean up the relational tables (Artist, Album, Genre, Key, Label,
        #    Color) to IDs actually referenced by surviving djmdContent rows.
        for lookup, fk in [
            ("djmdArtist", "ArtistID"),
            ("djmdAlbum", "AlbumID"),
            ("djmdGenre", "GenreID"),
            ("djmdKey", "KeyID"),
            ("djmdLabel", "LabelID"),
            ("djmdColor", "ColorID"),
        ]:
            try:
                cur.execute(
                    f'DELETE FROM "{lookup}" WHERE ID NOT IN '
                    f'(SELECT DISTINCT {fk} FROM djmdContent WHERE {fk} IS NOT NULL)'
                )
            except sqlite3.Error:
                pass  # table may not exist on older schemas

        cur.execute("COMMIT")
        cur.execute("VACUUM")

        # Collect final counts for the summary.
        counts: dict[str, int] = {}
        for t in ("djmdContent", "djmdSongPlaylist", "djmdPlaylist",
                  "djmdArtist", "djmdAlbum", "djmdGenre", "djmdKey"):
            try:
                counts[t] = cur.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                counts[t] = -1
        return counts
    finally:
        con.close()


# ------------------------------------------------------------------ readme


README_BODY = """# Rekordbox Test Fixture

`master.plain.db` is a **decrypted, pruned** copy of the live Rekordbox
`master.db`, committed to the repo so tests run offline against real data
shapes.

**Never** use this file for anything other than tests — it's deliberately
stripped down and should not round-trip back into a live Rekordbox install.

## Contents

~50 tracks covering:

- 10 normal healthy tracks
- 5 broken-link tracks (FolderPath → missing file)
- 5 streaming tracks (`spotify:`, `tidal:`, `http://`)
- 2 empty FolderPath
- 2 unicode in filename
- 2 very long paths
- 2 missing Artist
- 2 missing Album
- 4 rating-varied (0 / 2 / 4 / 5)
- remainder: diverse fillers
- 2–3 playlists with 3–5 tracks each

## Regenerate

```bash
python -m scripts.make_rb_fixture --force
```

Requires the live Rekordbox install's key to be cached by pyrekordbox
(`read_rekordbox6_options` returns `dp`). On CI the live DB is not
available, so this script cannot run there — CI consumes the checked-in
fixture only.

## Safety rails

- We always decrypt from a copy (`data/master.db.copy`) — the live
  `~/Library/Pioneer/rekordbox/master.db` is never touched.
- The decrypted fixture is written to a temp file first, then pruned and
  VACUUMed into `master.plain.db`.
- Foreign-key constraints are disabled during prune so table-delete order
  doesn't matter; integrity is then enforced by the cascading deletes we
  run against every table referencing `ContentID`/`PlaylistID`.
"""


def _write_readme() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    README_OUT.write_text(README_BODY, encoding="utf-8")


# ------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="scripts.make_rb_fixture",
        description="Build tests/fixtures/rekordbox/master.plain.db",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Overwrite existing fixture if present.",
    )
    args = p.parse_args(argv)

    if PLAIN_OUT.exists() and not args.force:
        return _fatal(
            f"{PLAIN_OUT} already exists. Pass --force to regenerate."
        )

    if not SRC_ENCRYPTED.exists():
        # Try refreshing from live.
        print(f"No {SRC_ENCRYPTED}; copying from live…")
        result = paths.copy_live_dbs()
        if result.get("rekordbox") is None:
            _write_readme()
            return _fatal(
                "Live Rekordbox DB not found. Fixture not generated. "
                f"See {README_OUT} for the blocker."
            )

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[1/5] decrypting {SRC_ENCRYPTED.name} → {PLAIN_TMP.name}")
    try:
        rekordbox_db.decrypt_to_plain(SRC_ENCRYPTED, PLAIN_TMP)
    except Exception as exc:
        _write_readme()
        return _fatal(
            f"SQLCipher decrypt failed: {exc!r}\n"
            f"See {README_OUT} for regen instructions + blocker notes."
        )

    print(f"[2/5] selecting ~{sum(TARGET_COMPOSITION.values())} edge-case IDs")
    ids, coverage = _select_ids(PLAIN_TMP)
    print(f"      picked {len(ids)} unique IDs")

    print(f"[3/5] pruning + cascading")
    # Work on a new file so _tmp_plain.db stays intact for debugging.
    if PLAIN_OUT.exists():
        PLAIN_OUT.unlink()
    shutil.copy2(PLAIN_TMP, PLAIN_OUT)
    counts = _prune(PLAIN_OUT, ids)

    print(f"[4/5] sanity checks")
    # All retained content IDs survived the prune.
    con = sqlite3.connect(PLAIN_OUT)
    try:
        actual = {str(r[0]) for r in con.execute("SELECT ID FROM djmdContent")}
        missing = [i for i in ids if i not in actual]
        if missing:
            return _fatal(f"prune dropped {len(missing)} intended IDs: {missing[:5]}")
    finally:
        con.close()

    print(f"[5/5] done — {PLAIN_OUT}")
    size_kb = PLAIN_OUT.stat().st_size / 1024
    print(f"\n  Fixture size : {size_kb:,.1f} KB")
    print("  Edge-case coverage (retained IDs per bucket):")
    for bucket, n in coverage.items():
        target = TARGET_COMPOSITION[bucket]
        flag = " " if n >= target else "!"
        print(f"   [{flag}] {bucket:<20} {n}/{target}")
    print("  Final row counts:")
    for t, c in counts.items():
        print(f"    {t:<20} {c}")

    # Clean up temp decrypted file.
    try:
        PLAIN_TMP.unlink()
    except FileNotFoundError:
        pass

    _write_readme()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
