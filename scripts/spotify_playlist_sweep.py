"""Sweep every Spotify playlist in the maintainer's account and rank them by library gap.

Answers "which playlists am I likely to want but do not have?" without
importing each one by hand.

Mini-PRD
--------
R1 ✔︎ List all playlists on the authed account (paged), reusing
     SpotifyClient.from_env so auth/token handling is never duplicated.
     [if SPOTIFY_CLIENT_ID is unset then fail fast naming doppler run ⛔️]
R2 ✔︎ For each playlist, count how many of its tracks are absent from the local
     library, matching on ISRC first then artist+title casefold.
     [if a playlist is fully owned then missing is 0 and gap_pct is 0.0 ⛔️]
R3 ✔︎ Print a table sorted by missing DESC and write a CSV. Read-only against
     both Spotify and the state DB.
     [if run twice with no library change then output is identical ⛔️]

Usage::

    doppler run --project general --config dev_personal -- \
      uv run --no-sync python scripts/spotify_playlist_sweep.py [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.spotify.client import SpotifyClient

# ----- config -------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
STATE_DB: Path = REPO_ROOT / "data" / "state" / "state.db"
OUT_CSV: Path = REPO_ROOT / "data" / "spotify" / "playlist-sweep.csv"
PAGE_SIZE: int = 50


@dataclass(frozen=True)
class PlaylistGap:
    name: str
    playlist_id: str
    owner: str
    total: int
    missing: int

    @property
    def gap_pct(self) -> float:
        return round(100.0 * self.missing / self.total, 1) if self.total else 0.0


# ----- helpers ------------------------------------------------------------


def _local_index(db: Path) -> tuple[set[str], set[str]]:
    """(isrcs, 'artist|title' keys) for every local track with a file."""
    if not db.exists():
        sys.exit(f"[ERROR] state DB not found: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT isrc, title, artists_json FROM tracks WHERE file_path IS NOT NULL"
        ).fetchall()
    finally:
        con.close()
    isrcs = {r[0].upper() for r in rows if r[0]}
    names = set()
    for _isrc, title, artists in rows:
        if not title:
            continue
        primary = (artists or "").strip('[]"').split('"')[0] if artists else ""
        names.add(f"{primary.casefold().strip()}|{title.casefold().strip()}")
    return isrcs, names


def rank_gaps(gaps: list[PlaylistGap]) -> list[PlaylistGap]:
    """Biggest absolute gap first; name breaks ties so runs are reproducible.

    Absolute missing count, not gap_pct: a 200-track playlist missing 90 is a
    bigger acquisition job than a 3-track playlist missing all 3, even though
    the latter is 100 percent missing.
    """
    return sorted(gaps, key=lambda g: (-g.missing, g.name))


def _all_playlists(sp) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        page = sp.current_user_playlists(limit=PAGE_SIZE, offset=offset)
        items = page.get("items") or []
        out.extend(p for p in items if p)
        if len(items) < PAGE_SIZE:
            return out
        offset += PAGE_SIZE


def _playlist_gap(sp, pl: dict, isrcs: set[str], names: set[str]) -> PlaylistGap:
    pid = pl["id"]
    missing = total = 0
    offset = 0
    while True:
        page = sp.playlist_items(
            pid,
            limit=100,
            offset=offset,
            fields="items(track(name,artists(name),external_ids(isrc))),total",
        )
        items = page.get("items") or []
        for it in items:
            tr = (it or {}).get("track") or {}
            if not tr.get("name"):
                continue
            total += 1
            isrc = ((tr.get("external_ids") or {}).get("isrc") or "").upper()
            artists = tr.get("artists") or [{}]
            key = (
                f"{(artists[0].get('name') or '').casefold().strip()}"
                f"|{tr['name'].casefold().strip()}"
            )
            if isrc and isrc in isrcs:
                continue
            if key in names:
                continue
            missing += 1
        if len(items) < 100:
            break
        offset += 100
    return PlaylistGap(
        name=pl.get("name") or "(untitled)",
        playlist_id=pid,
        owner=((pl.get("owner") or {}).get("display_name")) or "",
        total=total,
        missing=missing,
    )


# ----- main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="cap playlists scanned")
    args = ap.parse_args()

    isrcs, names = _local_index(STATE_DB)
    print(f"[OK] local index: {len(isrcs)} ISRCs, {len(names)} artist|title keys")

    sp = SpotifyClient.from_env()._sp
    playlists = _all_playlists(sp)
    if args.limit:
        playlists = playlists[: args.limit]
    print(f"[OK] playlists on account: {len(playlists)}")

    gaps: list[PlaylistGap] = []
    for i, pl in enumerate(playlists, 1):
        try:
            gap = _playlist_gap(sp, pl, isrcs, names)
        except Exception as exc:
            print(f"  [WARN] {pl.get('name')!r}: {exc}")
            continue
        gaps.append(gap)
        print(f"  {i:>3}/{len(playlists)}  {gap.missing:>4} missing / "
              f"{gap.total:>4}  {gap.name[:48]}")

    gaps = rank_gaps(gaps)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(("missing", "total", "gap_pct", "name", "owner", "playlist_id"))
        for g in gaps:
            w.writerow((g.missing, g.total, g.gap_pct, g.name, g.owner, g.playlist_id))

    print(f"\n[OK] csv: {OUT_CSV}")
    print("\n  biggest gaps:")
    for g in gaps[:20]:
        print(f"    {g.missing:>4} / {g.total:>4}  ({g.gap_pct:>5.1f}%)  {g.name[:46]}")


if __name__ == "__main__":
    main()
