"""Rank the library's missing-file gaps by how much the maintainer actually plays them.

Mini-PRD
--------
R1 ✔︎ Read djmdContent (static decrypted copy) joined to artist names, restricted
     to tracks the maintainer has actually played (DJPlayCount > 0).
     [if a track has DJPlayCount 0 then it must not appear ⛔️]
     [if the decrypted copy is absent then exit nonzero, never silently empty ⛔️]
R2 ✔︎ Classify each track's file residency using the shared fs_residency helper,
     so iCloud dataless stubs count as MISSING rather than present.
     [if a path is a 0-block iCloud stub then status is missing-stub ⛔️]
     [if a path lives under an unreachable home (an os.pathsep-separated list
      configured via MDT_DEAD_HOME_PREFIXES, e.g. "/Users/oldname/") then
      status is missing-dead-machine ⛔️]
     [if MDT_DEAD_HOME_PREFIXES is unset then classification exits nonzero
      rather than silently matching nothing ⛔️]
R3 ✔︎ Emit a CSV ranked by play count DESC, ready to paste into a sheet, plus a
     terminal summary. Read-only: touches no live Rekordbox DB.
     [if two runs happen back to back then output is byte-identical ⛔️]

Usage::

    MDT_DEAD_HOME_PREFIXES=/Users/oldname/ \
        uv run --no-sync python scripts/missing_by_playcount.py [--min-plays N]

MDT_DEAD_HOME_PREFIXES names this machine's stale/unreachable home
directories (os.pathsep-separated, e.g. "/Users/oldname/:/Users/older/") --
a value that is machine-specific and MUST NOT be hardcoded here (#910).
Required: the script fails fast if it is unset rather than silently
matching zero rows.
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import stat as stat_module
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# ----- config -------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
MASTER_DB: Path = REPO_ROOT / "data" / "master.plain.db"
OUT_CSV: Path = REPO_ROOT / "data" / "reconcile" / "missing-by-playcount.csv"
#: Env var naming this machine's stale-home prefixes. No default: a real
#: home name must never be hardcoded in tracked code (#910).
DEAD_HOME_PREFIXES_ENV: str = "MDT_DEAD_HOME_PREFIXES"

CSV_COLUMNS: tuple[str, ...] = (
    "plays",
    "rating",
    "title",
    "artist",
    "status",
    "path",
)


@dataclass(frozen=True)
class GapRow:
    plays: int
    rating: int
    title: str
    artist: str
    status: str
    path: str


# ----- helpers ------------------------------------------------------------


def _is_materialised(path: Path) -> bool:
    """True when path is a regular file holding local bytes.

    Mirrors apps.shared.fs_residency (not on every branch, so inlined here).
    An iCloud dataless stub reports a logical size with zero allocated blocks;
    stat only -- never open/read, which would trigger a bird materialise.
    """
    try:
        st = path.stat()
    except OSError:
        return False
    if not stat_module.S_ISREG(st.st_mode):
        return False
    return not (st.st_size > 0 and st.st_blocks == 0)


def _dead_home_prefixes() -> tuple[str, ...]:
    """Read this machine's stale-home prefixes from the environment.

    Read fresh (not cached at import time) so a test can set/unset the
    variable per case. No hidden default: an unset variable means the
    caller has not configured which home is dead on this machine, and
    silently matching nothing would mislabel every dead-machine row as
    a live-but-missing file (#910).
    """
    raw = os.environ.get(DEAD_HOME_PREFIXES_ENV)
    if not raw:
        sys.exit(
            f"[ERROR] {DEAD_HOME_PREFIXES_ENV} is not set -- export this "
            "machine's stale home prefix(es), os.pathsep-separated (e.g. "
            f"{DEAD_HOME_PREFIXES_ENV}=/Users/oldname/)"
        )
    prefixes = tuple(p for p in raw.split(os.pathsep) if p)
    if not prefixes:
        sys.exit(f"[ERROR] {DEAD_HOME_PREFIXES_ENV} is set but contains no usable prefixes")
    return prefixes


def _classify(path_text: str | None) -> str:
    """Residency verdict for one Rekordbox FolderPath."""
    if not path_text:
        return "missing-no-path"
    if path_text.startswith(_dead_home_prefixes()):
        return "missing-dead-machine"
    path = Path(path_text)
    if _is_materialised(path):
        return "present"
    if path.exists():
        return "missing-stub"
    return "missing-gone"


def _fetch_played_tracks(db: Path, min_plays: int) -> list[tuple]:
    if not db.exists():
        sys.exit(f"[ERROR] decrypted Rekordbox copy not found: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(
            """
            SELECT c.DJPlayCount, COALESCE(c.Rating, 0), COALESCE(c.Title, ''),
                   COALESCE(a.Name, ''), c.FolderPath
            FROM djmdContent c
            LEFT JOIN djmdArtist a ON a.ID = c.ArtistID
            WHERE c.DJPlayCount >= ?
            ORDER BY c.DJPlayCount DESC, c.Title ASC
            """,
            (min_plays,),
        ).fetchall()
    finally:
        con.close()


def _build_rows(raw: list[tuple]) -> list[GapRow]:
    rows: list[GapRow] = []
    for plays, rating, title, artist, folder_path in raw:
        status = _classify(folder_path)
        if status == "present":
            continue
        rows.append(
            GapRow(
                plays=plays,
                rating=rating // 51 if rating else 0,  # RB stores 0/51/.../255
                title=title,
                artist=artist,
                status=status,
                path=folder_path or "",
            )
        )
    return rows


def _write_csv(rows: list[GapRow], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        for r in rows:
            writer.writerow([r.plays, r.rating, r.title, r.artist, r.status, r.path])


# ----- main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-plays", type=int, default=1)
    args = ap.parse_args()

    raw = _fetch_played_tracks(MASTER_DB, args.min_plays)
    if not raw:
        sys.exit(f"[ERROR] zero tracks with DJPlayCount >= {args.min_plays}")
    rows = _build_rows(raw)
    _write_csv(rows, OUT_CSV)

    by_status = Counter(r.status for r in rows)
    print(f"[OK] played tracks (>= {args.min_plays} plays): {len(raw)}")
    print(f"[OK] of those, MISSING a playable file: {len(rows)}")
    for status, n in by_status.most_common():
        print(f"       {status}: {n}")
    print(f"[OK] plays lost to missing files: {sum(r.plays for r in rows)}")
    print(f"[OK] csv: {OUT_CSV}")
    print("\n  top 15 by play count:")
    for r in rows[:15]:
        print(f"    {r.plays:>3}x  {r.artist[:28]:<28} {r.title[:36]:<36} {r.status}")


if __name__ == "__main__":
    main()
