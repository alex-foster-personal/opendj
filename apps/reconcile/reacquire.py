"""Re-acquisition worklist for the ``absent-no-audio`` bucket.

Entry point::

    python -m apps.reconcile reacquire --data-dir /Users/user/code/music-dj-tools/data
    python -m apps.reconcile reacquire --min-playlists 1     # only wanted tracks
    python -m apps.reconcile reacquire --out /tmp/list.csv

These are the rows whose recorded audio file is NOT on this Mac and for which
the tiered matcher found NO candidate anywhere under the indexed roots. They
are not a matcher failure and they are not an unmounted drive: the audio is
genuinely gone from this machine, so the only route back is to re-acquire the
file. This module turns that bucket into a ranked shopping list, most-wanted
first, where "wanted" means "referenced by the most playlists".

READ-ONLY, like :mod:`apps.reconcile.match`. The state DB is opened through
:func:`apps.shared.state.db.open_ro` (``mode=ro`` URI plus ``PRAGMA
query_only``), so an ``UPDATE`` raises at execute time rather than silently
landing. No audio file is read, moved, renamed, deleted or re-encoded, and no
track row is removed. The only output is one CSV.

Deliberately NOT in scope: ``awaiting-volume`` rows. A row under an unmounted
``/Volumes/<name>`` is presumed fine on an offline drive; listing it as
something to re-download would tell the maintainer to re-buy music he already owns.
``relinkable-ambiguous`` is also excluded -- those have a candidate on disk and
belong in the review CSV, not the shopping list.

Requirements (mini-PRD)
-----------------------
1. One CSV row per ``absent-no-audio`` track, carrying title, artists, album,
   ISRC, duration, the original recorded path and the referencing playlists. OK
   - [if] a track sits in the bucket with two playlist memberships [then] its
     ``playlist_count`` is 2 and ``playlists`` holds both names, ``|``-joined.
   - [if] a track sits in the bucket with no membership at all [then] it still
     appears, with ``playlist_count`` 0 and an empty ``playlists`` cell.
   - [if] ``artists_json`` holds ``["A", "B"]`` [then] the ``artists`` cell
     reads ``A, B`` and not the raw JSON.
2. Ordered most-wanted first, deterministically. OK
   - [if] one track is in 3 playlists and another in 1 [then] the 3-playlist
     row is written first.
   - [if] two tracks tie on ``playlist_count`` [then] they are ordered by
     title, then stable_id, so two runs produce byte-identical files.
3. The file explains its own columns. OK
   - [if] the CSV is opened cold [then] the leading ``#`` comment block names
     every column, the bucket definition and the generating command.
   - [if] a consumer skips lines beginning ``#`` [then] what remains is a
     plain single-header CSV that ``csv.DictReader`` parses.
4. Never widens the blast radius of reconcile. OK
   - [if] an ``UPDATE`` is attempted on the handle this module opens [then]
     sqlite3 raises (asserted by test).
   - [if] the run completes [then] the state.db mtime is unchanged.

Regression contract: read-only worklist behavior and deterministic ordering.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.reconcile.index_disk import CACHE_PATH, DEFAULT_ROOTS, build_index
from apps.reconcile.match import RowResult, classify_rows, load_track_rows
from apps.shared import paths
from apps.shared.state import db as state_db_mod

console = Console(width=120)

# The bucket this worklist is about. Kept as a module constant rather than a
# CLI flag: the other buckets each have a different remedy (relink, mount the
# drive, fix the path), and offering them here would invite the wrong action.
TARGET_BUCKET: str = "absent-no-audio"

PLAYLIST_SEPARATOR: str = " | "

COLUMNS: tuple[str, ...] = (
    "playlist_count",
    "title",
    "artists",
    "album",
    "isrc",
    "duration_ms",
    "duration_hms",
    "original_path",
    "playlists",
    "stable_id",
)

COLUMN_MEANINGS: tuple[tuple[str, str], ...] = (
    (
        "playlist_count",
        "how many DISTINCT playlists reference this track (the sort key, descending)",
    ),
    ("title", "tracks.title as recorded in state.db"),
    ("artists", "tracks.artists_json flattened to a comma-joined string"),
    ("album", "tracks.album as recorded in state.db"),
    ("isrc", "tracks.isrc, uppercased and stripped; blank when unknown"),
    ("duration_ms", "tracks.duration_ms as recorded; blank when unknown"),
    ("duration_hms", "duration_ms rendered as h:mm:ss for a human scanning the list"),
    ("original_path", "the file_path state.db still holds; the file is NOT there now"),
    ("playlists", f"referencing playlist names, joined by '{PLAYLIST_SEPARATOR.strip()}'"),
    ("stable_id", "tracks.stable_id, the join key back into state.db"),
)


# ----- data --------------------------------------------------------------


@dataclass(slots=True)
class ReacquireEntry:
    """One line of the worklist."""

    stable_id: str
    title: str
    artists: str
    album: str
    isrc: str
    duration_ms: int | None
    original_path: str
    playlists: list[str]

    @property
    def playlist_count(self) -> int:
        return len(self.playlists)

    @property
    def duration_hms(self) -> str:
        if self.duration_ms is None:
            return ""
        total = round(self.duration_ms / 1000)
        hours, rest = divmod(total, 3600)
        minutes, seconds = divmod(rest, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def as_row(self) -> list[str]:
        return [
            str(self.playlist_count),
            self.title,
            self.artists,
            self.album,
            self.isrc,
            "" if self.duration_ms is None else str(self.duration_ms),
            self.duration_hms,
            self.original_path,
            PLAYLIST_SEPARATOR.join(self.playlists),
            self.stable_id,
        ]


# ----- reads -------------------------------------------------------------


def load_playlist_memberships(state_db: Path) -> dict[str, list[str]]:
    """``{stable_id: [playlist name, ...]}`` read-only, names sorted.

    Counts DISTINCT playlists, not membership rows. A track appearing at
    multiple positions in the same playlist counts once: repeated membership
    is not extra demand for the file. Two DIFFERENT playlists that happen to
    share a name are still two references and appear twice.

    A membership whose playlist row is missing would be a referential hole in
    state.db; it is surfaced as ``<orphan playlist {id}>`` rather than dropped,
    because silently shrinking a want-count would understate demand.
    """
    conn = state_db_mod.open_ro(state_db)
    try:
        names = dict(
            conn.execute(
                "SELECT playlist_id, name FROM playlists WHERE deleted_at IS NULL"
            ).fetchall()
        )
        pairs = conn.execute(
            "SELECT DISTINCT stable_id, playlist_id FROM playlist_memberships "
            "WHERE deleted_at IS NULL"
        ).fetchall()
    finally:
        conn.close()

    by_track: dict[str, list[str]] = defaultdict(list)
    for stable_id, playlist_id in pairs:
        name = names.get(playlist_id)
        if name is None:
            name = f"<orphan playlist {playlist_id}>"
        by_track[stable_id].append(name)
    return {sid: sorted(found) for sid, found in by_track.items()}


def build_entries(
    results: Sequence[RowResult],
    memberships: dict[str, list[str]],
    *,
    row_meta: dict[str, tuple[str, str, str, str, int | None]],
    min_playlists: int = 0,
) -> list[ReacquireEntry]:
    """Absent-no-audio rows as ranked entries, most-wanted first.

    ``row_meta`` maps stable_id to (title, artists, album, isrc, duration_ms);
    a result whose stable_id is absent from it means the classifier and the row
    reader disagree about the row set, which is a bug rather than a blank cell,
    so it raises.
    """
    entries: list[ReacquireEntry] = []
    for res in results:
        if res.bucket != TARGET_BUCKET:
            continue
        meta = row_meta.get(res.stable_id)
        if meta is None:
            raise AssertionError(
                f"classified row {res.stable_id!r} has no corresponding track row"
            )
        title, artists, album, isrc, duration_ms = meta
        playlists = memberships.get(res.stable_id, [])
        if len(playlists) < min_playlists:
            continue
        entries.append(
            ReacquireEntry(
                stable_id=res.stable_id,
                title=title,
                artists=artists,
                album=album,
                isrc=isrc,
                duration_ms=duration_ms,
                original_path=res.file_path or "",
                playlists=playlists,
            )
        )
    entries.sort(key=lambda e: (-e.playlist_count, e.title.casefold(), e.stable_id))
    return entries


def row_meta_from_rows(rows: Sequence[object]) -> dict[str, tuple[str, str, str, str, int | None]]:
    """Pull the display fields off :class:`~apps.reconcile.match.TrackRow` objects."""
    meta: dict[str, tuple[str, str, str, str, int | None]] = {}
    for row in rows:
        meta[row.stable_id] = (  # type: ignore[attr-defined]
            (row.title or "").strip(),  # type: ignore[attr-defined]
            (row.artist or "").strip(),  # type: ignore[attr-defined]
            "",
            (row.isrc or "").strip(),  # type: ignore[attr-defined]
            row.duration_ms,  # type: ignore[attr-defined]
        )
    return meta


def load_albums(state_db: Path) -> dict[str, str]:
    """``{stable_id: album}``; ``TrackRow`` does not carry album, this does."""
    conn = state_db_mod.open_ro(state_db)
    try:
        return {
            sid: (album or "").strip()
            for sid, album in conn.execute(
                "SELECT stable_id, album FROM tracks WHERE deleted_at IS NULL"
            )
        }
    finally:
        conn.close()


# ----- writing -----------------------------------------------------------


def header_comment_lines(*, generated_at: str, total: int, command: str) -> list[str]:
    """The self-documenting ``#`` block that opens the CSV."""
    lines = [
        f"# reacquire worklist -- state.db rows in the '{TARGET_BUCKET}' bucket",
        f"# generated {generated_at} by: {command}",
        f"# {total} rows, most-wanted first (playlist_count descending)",
        "#",
        f"# '{TARGET_BUCKET}' means: the recorded file_path does not resolve on this Mac AND",
        "# the tiered matcher found no candidate under the indexed roots. This is NOT an",
        "# unmounted drive (that is 'awaiting-volume') and NOT a weak match (that is",
        "# 'relinkable-ambiguous'). The audio is gone from this machine; re-acquire it.",
        "#",
        "# playlist_count counts DISTINCT playlists. A track sitting twice in the SAME",
        "# playlist counts once (that is a library dedup issue, not extra demand). A name",
        "# appearing twice in the playlists cell means two DIFFERENT playlist rows share it.",
        "#",
        "# Columns:",
    ]
    width = max(len(name) for name, _ in COLUMN_MEANINGS)
    for name, meaning in COLUMN_MEANINGS:
        lines.append(f"#   {name.ljust(width)}  {meaning}")
    lines.append("#")
    lines.append("# Lines beginning '#' are comments; the next line is the CSV header.")
    return lines


def write_worklist(
    entries: Sequence[ReacquireEntry], out: Path, *, generated_at: str, command: str
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        for line in header_comment_lines(
            generated_at=generated_at, total=len(entries), command=command
        ):
            fh.write(line + "\n")
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        for entry in entries:
            writer.writerow(entry.as_row())


def read_worklist(path: Path) -> list[dict[str, str]]:
    """Round-trip helper: skip the ``#`` block, parse the rest as a CSV."""
    with path.open(newline="", encoding="utf-8") as fh:
        body = [line for line in fh if not line.startswith("#")]
    return list(csv.DictReader(body))


# ----- cli ---------------------------------------------------------------


def _default_out(data_dir: Path, today: dt.date) -> Path:
    return data_dir / "state" / f"reacquire-list-{today.isoformat()}.csv"


def _print_summary(entries: Sequence[ReacquireEntry], out: Path, head: int) -> None:
    wanted = sum(1 for e in entries if e.playlist_count > 0)
    console.print(
        f"[bold]{len(entries)}[/bold] rows in '{TARGET_BUCKET}'; "
        f"[bold]{wanted}[/bold] referenced by at least one playlist, "
        f"{len(entries) - wanted} referenced by none"
    )
    if not entries:
        return
    table = Table(title=f"Most wanted (top {min(head, len(entries))})")
    table.add_column("Playlists", justify="right")
    table.add_column("Title", overflow="fold")
    table.add_column("Artists", overflow="fold")
    for entry in entries[:head]:
        table.add_row(str(entry.playlist_count), entry.title or "(untitled)", entry.artists)
    console.print(table)
    console.print(f"Worklist written to {out}")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.reconcile reacquire",
        description=(
            f"Ranked re-acquisition worklist for the '{TARGET_BUCKET}' bucket. "
            "Read-only: writes one CSV, never state.db, never audio."
        ),
    )
    p.add_argument("--data-dir", type=Path, default=paths.DATA_DIR)
    p.add_argument(
        "--roots",
        nargs="+",
        type=Path,
        default=None,
        help=f"disk roots to index (default: {' '.join(str(r) for r in DEFAULT_ROOTS)})",
    )
    p.add_argument("--cache", type=Path, default=None, help="disk index cache json")
    p.add_argument("--rebuild-index", action="store_true", help="re-read every tag")
    p.add_argument(
        "--min-playlists",
        type=int,
        default=0,
        help="drop rows referenced by fewer than N playlists (default 0, keep all)",
    )
    p.add_argument("--out", type=Path, default=None, help="output CSV path")
    p.add_argument("--head", type=int, default=15, help="preview rows to print")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    state_db = args.data_dir / "state" / "state.db"
    if not state_db.exists():
        console.print(f"[red]state DB not found at {state_db}[/red]")
        return 1
    rb_db = args.data_dir / "master.plain.db"

    console.print(f"[bold]Step 1:[/bold] reading rows from {state_db} (read-only)")
    rows = load_track_rows(state_db, rb_db=rb_db if rb_db.exists() else None)
    console.print(f"  {len(rows)} track rows")

    roots = args.roots if args.roots else list(DEFAULT_ROOTS)
    cache = args.cache if args.cache else args.data_dir / "state" / CACHE_PATH.name
    console.print(f"[bold]Step 2:[/bold] indexing {', '.join(str(r) for r in roots)}")
    index, stats = build_index(roots, cache_path=cache, rebuild=args.rebuild_index)
    console.print(f"  {stats.walked} audio files, {stats.tag_reads} tag reads")

    console.print("[bold]Step 3:[/bold] classifying and joining playlists")
    results = classify_rows(rows, index)
    memberships = load_playlist_memberships(state_db)
    meta = row_meta_from_rows(rows)
    albums = load_albums(state_db)
    for sid, fields in meta.items():
        title, artists, _, isrc, duration_ms = fields
        meta[sid] = (title, artists, albums.get(sid, ""), isrc, duration_ms)

    entries = build_entries(
        results, memberships, row_meta=meta, min_playlists=args.min_playlists
    )
    out = args.out if args.out else _default_out(args.data_dir, dt.date.today())
    command = "python -m apps.reconcile reacquire --data-dir " + str(args.data_dir)
    if args.min_playlists:
        command += f" --min-playlists {args.min_playlists}"
    write_worklist(
        entries,
        out,
        generated_at=dt.datetime.now().astimezone().strftime("%a %d %b %Y %H:%M %z"),
        command=command,
    )
    console.print(f"[bold]Step 4:[/bold] {os.fspath(out)}")
    _print_summary(entries, out, args.head)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
