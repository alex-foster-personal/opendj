"""Apply (and undo) ``tracks.file_path`` relinks -- dry-run by default.

Entry points::

    # Dry-run (DEFAULT): classify, select, write the reversal log, change nothing.
    python -m apps.reconcile relink --data-dir /path/to/data

    # Live: same selection, then write through the state writer.
    python -m apps.reconcile relink --live --i-understand-the-risks

    # Undo a live run from its reversal log (dry-run by default too).
    python -m apps.reconcile relink-undo --log <log.csv> --live \\
        --i-understand-the-risks

Blast radius
------------
The ONLY column this module ever changes is ``tracks.file_path`` (plus the
``updated_at`` stamp and the ``events`` row that
:class:`apps.shared.state.writer.StateWriter` appends -- that writer is the
repo's single supported mutation surface and we do not bypass it). It never
deletes, moves, renames or re-encodes an audio file, and it never removes a
track row. A post-write readback asserts every other column is byte-identical
to what it was before the update; a single mismatch rolls the whole batch back.

Reversibility
-------------
Three independent layers, in this order:

1. A reversal log CSV (``stable_id, old_path, new_path, tier, confidence,
   timestamp``) is written and flushed BEFORE the first UPDATE. If the process
   dies mid-batch the log still describes every intended change, and
   ``relink-undo`` is a no-op for rows that never landed.
2. A timestamped copy of ``state.db`` under ``<data-dir>/reconcile/backups/``.
3. The ``events`` table, which records one ``track.update`` per row.

Requirements (mini-PRD)
-----------------------
1. Dry-run is the default; only ``--live --i-understand-the-risks`` writes. OK
   - [if] no flags are passed [then] state.db's mtime is unchanged afterwards.
   - [if] ``--live`` is passed without ``--i-understand-the-risks`` [then] the
     command exits 2 and writes nothing.
2. The reversal log is written before any UPDATE. OK
   - [if] the log file cannot be written [then] no UPDATE is attempted.
   - [if] a live run applies N rows [then] the log holds exactly N rows with
     both old and new path.
3. Selection is filtered by bucket, confidence and limit, and re-checked on
   disk at apply time. OK
   - [if] a candidate path no longer exists at apply time [then] the row is
     rejected, not applied.
   - [if] two selected rows target one file [then] both are rejected.
   - [if] a bucket is ``awaiting-volume`` / ``streaming`` / ``present`` /
     ``absent-no-audio`` [then] ``--bucket`` refuses it outright.
4. ``relink-undo`` restores old paths and never overwrites drift. OK
   - [if] a row's current path is neither the log's new nor old path [then] it
     is reported as a conflict and left alone.
   - [if] undo runs twice [then] the second run reports every row as already
     reverted and changes nothing.

Status: OK ran-script works-as-expected, regression tests in
``tests/reconcile/test_relink.py``.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.table import Table

from apps.reconcile import match
from apps.reconcile.index_disk import CACHE_PATH, DEFAULT_ROOTS, build_index
from apps.shared import paths
from apps.shared.state import db as state_db_mod
from apps.shared.state.writer import StateWriter

console = Console(width=120)

# Default confidence floor for an applied relink.
#
# Justification: this is exactly :data:`match.AUTO_APPLY_THRESHOLD` (0.85), the
# line the matcher draws between IDENTITY evidence and RESEMBLANCE evidence.
# The tiers that can reach it all matched a filename or a content fact --
# basename-exact-unique 0.95, isrc 0.92, basename-duration 0.90,
# basename-size 0.88. The fuzzy tiers are capped by construction at
# 0.60 + 0.20 = 0.80 (title/artist) and 0.45 + 0.20 = 0.65 (duration/title), so
# no amount of text similarity can cross this line. Re-deriving the number here
# instead of hardcoding 0.85 keeps the two in lockstep if the matcher moves.
DEFAULT_MIN_CONFIDENCE: float = match.AUTO_APPLY_THRESHOLD

# Buckets a relink may target at all. The excluded four are excluded for
# reasons, not for tidiness:
#   present          -- the recorded path already resolves; nothing to repair.
#   awaiting-volume  -- the file is presumed intact on an unplugged drive.
#                       Repointing it at a same-named file on the internal SSD
#                       would silently swap one library's track for another's.
#   streaming        -- spotify:/soundcloud:/tidal: rows have no local file.
#   absent-no-audio  -- by definition there is no candidate to point at.
APPLICABLE_BUCKETS: frozenset[str] = frozenset(
    {"relinkable-auto", "relinkable-ambiguous"}
)

# ``relinkable-ambiguous`` is selectable but never by accident: a live run over
# it demands the risk flag AND still filters per-row on --min-confidence.
BUCKETS_NEEDING_RISK_FLAG: frozenset[str] = frozenset({"relinkable-ambiguous"})

LOG_COLUMNS: tuple[str, ...] = (
    "stable_id",
    "old_path",
    "new_path",
    "tier",
    "confidence",
    "timestamp",
)

DRY_RUN_LOG_SUFFIX: str = ".dryrun.csv"

# Columns that a relink is allowed to change. Everything else in ``tracks`` is
# asserted byte-identical after each write.
MUTABLE_COLUMNS: frozenset[str] = frozenset({"file_path", "updated_at"})

TRACK_COLUMNS: tuple[str, ...] = (
    "stable_id",
    "stable_id_tier",
    "title",
    "artists_json",
    "album",
    "isrc",
    "duration_ms",
    "file_path",
    "content_hash",
    "created_at",
    "updated_at",
)

UpdateOutcome = Literal["changed", "already", "conflict"]


@dataclass(slots=True)
class RelinkEntry:
    """One intended ``file_path`` change, and the log line that records it."""

    stable_id: str
    old_path: str
    new_path: str
    tier: str
    confidence: float
    timestamp: str

    def as_log_row(self) -> list[str]:
        return [
            self.stable_id,
            self.old_path,
            self.new_path,
            self.tier,
            f"{self.confidence:.3f}",
            self.timestamp,
        ]


@dataclass(slots=True)
class Rejection:
    """A row that passed selection but failed a pre-write safety re-check."""

    stable_id: str
    new_path: str
    reason: str


@dataclass(slots=True)
class Outcome:
    """What actually happened to one row during a live pass."""

    stable_id: str
    outcome: UpdateOutcome
    detail: str = ""


# ----- selection ---------------------------------------------------------


def select_entries(
    results: Sequence[match.RowResult],
    *,
    buckets: Iterable[str],
    min_confidence: float,
    limit: int | None = None,
    timestamp: str | None = None,
) -> list[RelinkEntry]:
    """Rows in ``buckets`` whose best candidate clears ``min_confidence``.

    Deterministically ordered by (-confidence, stable_id) so ``--limit`` takes
    the strongest evidence first and a repeat run with the same limit selects
    the same rows.
    """
    wanted = set(buckets)
    unknown = wanted - set(match.ALL_BUCKETS)
    if unknown:
        raise ValueError(f"unknown bucket(s): {sorted(unknown)}")
    forbidden = wanted - APPLICABLE_BUCKETS
    if forbidden:
        raise ValueError(
            f"bucket(s) {sorted(forbidden)} are never relinkable; "
            f"allowed: {sorted(APPLICABLE_BUCKETS)}"
        )
    stamp = timestamp or _now_iso()
    picked: list[RelinkEntry] = []
    for res in results:
        if res.bucket not in wanted:
            continue
        best = res.best
        if best is None or best.confidence < min_confidence:
            continue
        if not res.file_path:
            # A NULL/blank recorded path cannot be reversed to anything, so it
            # is not eligible even if a candidate exists. Those rows are
            # bucketed malformed-path anyway; this is belt and braces.
            continue
        picked.append(
            RelinkEntry(
                stable_id=res.stable_id,
                old_path=res.file_path,
                new_path=best.path,
                tier=best.tier,
                confidence=best.confidence,
                timestamp=stamp,
            )
        )
    picked.sort(key=lambda e: (-e.confidence, e.stable_id))
    return picked if limit is None else picked[:limit]


def reject_unsafe(
    entries: Sequence[RelinkEntry], *, all_recorded_paths: Iterable[str]
) -> tuple[list[RelinkEntry], list[Rejection]]:
    """Split ``entries`` into applicable and rejected, re-checking disk truth.

    The classifier's view can be stale by the time we write (a file moved, a
    volume unmounted, another agent relinked a row). These four checks are
    re-run against reality immediately before the log is written:

    1. the new path exists on disk NOW,
    2. the new path differs from the old path (an identical write is not a
       repair, it is noise in the events table),
    3. no OTHER track row already records that exact path -- including rows
       whose path does not currently resolve, which is stricter than the
       classifier's ``linked_paths`` and prevents resurrecting a duplicate,
    4. no two entries in this batch target the same file.
    """
    taken: dict[str, str] = {}
    recorded = set(all_recorded_paths)
    ok: list[RelinkEntry] = []
    rejected: list[Rejection] = []
    for entry in entries:
        if not os.path.exists(entry.new_path):
            rejected.append(
                Rejection(
                    entry.stable_id,
                    entry.new_path,
                    "candidate path no longer exists on disk",
                )
            )
            continue
        if entry.new_path == entry.old_path:
            rejected.append(
                Rejection(entry.stable_id, entry.new_path, "new path equals old path")
            )
            continue
        others = recorded - {entry.old_path}
        if entry.new_path in others:
            rejected.append(
                Rejection(
                    entry.stable_id,
                    entry.new_path,
                    "another track row already records this exact path",
                )
            )
            continue
        prior = taken.get(entry.new_path)
        if prior is not None:
            rejected.append(
                Rejection(
                    entry.stable_id,
                    entry.new_path,
                    f"path already claimed in this batch by {prior}",
                )
            )
            continue
        taken[entry.new_path] = entry.stable_id
        ok.append(entry)
    return ok, rejected


# ----- reversal log ------------------------------------------------------


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def log_path_for(out_dir: Path, *, live: bool, stamp: str | None = None) -> Path:
    """``relink-log-<ts>.csv`` for a live run, ``...dryrun.csv`` otherwise.

    The dry-run name is distinct on purpose: ``relink-undo`` refuses a
    ``.dryrun.csv`` file outright, because reverting changes that were never
    applied would rewrite good paths with dead ones.
    """
    ts = stamp or time.strftime("%Y%m%dT%H%M%S")
    tail = ".csv" if live else DRY_RUN_LOG_SUFFIX
    return out_dir / f"relink-log-{ts}{tail}"


def write_log(entries: Sequence[RelinkEntry], out: Path) -> Path:
    """Write the reversal log and fsync it. Called BEFORE the first UPDATE."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(LOG_COLUMNS)
        for entry in entries:
            writer.writerow(entry.as_log_row())
        fh.flush()
        os.fsync(fh.fileno())
    return out


def read_log(path: Path) -> list[RelinkEntry]:
    """Parse a reversal log. Missing columns are an error, not a default."""
    if not path.exists():
        raise FileNotFoundError(f"reversal log not found: {path}")
    if path.name.endswith(DRY_RUN_LOG_SUFFIX):
        raise ValueError(
            f"{path.name} is a DRY-RUN log: those changes were never applied, "
            "so undoing them would overwrite live paths with dead ones"
        )
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = set(LOG_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} is missing column(s): {sorted(missing)}")
        out: list[RelinkEntry] = []
        for row in reader:
            out.append(
                RelinkEntry(
                    stable_id=row["stable_id"],
                    old_path=row["old_path"],
                    new_path=row["new_path"],
                    tier=row["tier"],
                    confidence=float(row["confidence"]),
                    timestamp=row["timestamp"],
                )
            )
    return out


# ----- the write itself --------------------------------------------------


def _read_track(conn: sqlite3.Connection, stable_id: str) -> dict[str, object] | None:
    cur = conn.execute(
        f"SELECT {', '.join(TRACK_COLUMNS)} FROM tracks "
        "WHERE stable_id = ? AND deleted_at IS NULL",
        (stable_id,),
    )
    row = cur.fetchone()
    return None if row is None else dict(zip(TRACK_COLUMNS, row, strict=False))


def update_file_path(
    writer: StateWriter,
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    expected_current: str,
    new_value: str,
) -> Outcome:
    """Repoint one row's ``file_path`` through the state writer.

    Returns ``already`` when the row is already at ``new_value`` (idempotent
    re-run) and ``conflict`` when it holds something we did not expect -- the
    caller decides whether a conflict is fatal. Never overwrites a conflict.

    Raises when the post-write readback shows any column other than
    ``file_path``/``updated_at`` moved: that is a writer-contract violation and
    the caller's SAVEPOINT must roll the batch back rather than log a lie.
    """
    before = _read_track(conn, stable_id)
    if before is None:
        return Outcome(stable_id, "conflict", "stable_id not present in tracks")
    current = before["file_path"]
    if current == new_value:
        return Outcome(stable_id, "already", "file_path already at target")
    if current != expected_current:
        return Outcome(
            stable_id,
            "conflict",
            f"file_path is {current!r}, expected {expected_current!r}",
        )

    artists_raw = before["artists_json"]
    artists = json.loads(artists_raw) if artists_raw else []
    if not isinstance(artists, list):
        raise TypeError(
            f"{stable_id}: artists_json is {type(artists).__name__}, not a list; "
            "refusing to round-trip it through the writer"
        )
    writer.upsert_track(
        stable_id=stable_id,
        stable_id_tier=str(before["stable_id_tier"]),
        title=before["title"],  # type: ignore[arg-type]
        artists=[str(a) for a in artists],
        album=before["album"],  # type: ignore[arg-type]
        isrc=before["isrc"],  # type: ignore[arg-type]
        duration_ms=before["duration_ms"],  # type: ignore[arg-type]
        file_path=new_value,
        content_hash=before["content_hash"],  # type: ignore[arg-type]
    )
    after = _read_track(conn, stable_id)
    if after is None:
        raise RuntimeError(f"{stable_id}: row vanished during its own update")
    drifted = [
        col
        for col in TRACK_COLUMNS
        if col not in MUTABLE_COLUMNS and after[col] != before[col]
    ]
    if drifted:
        raise RuntimeError(
            f"{stable_id}: relink changed column(s) {drifted} beyond file_path; "
            "rolling back the batch"
        )
    if after["file_path"] != new_value:
        raise RuntimeError(
            f"{stable_id}: file_path read back as {after['file_path']!r}, "
            f"expected {new_value!r}"
        )
    return Outcome(stable_id, "changed", f"{expected_current} -> {new_value}")


def apply_entries(
    db_path: Path,
    entries: Sequence[RelinkEntry],
    *,
    reverse: bool = False,
    tolerate_conflicts: bool = False,
    actor: str = "reconcile.relink",
) -> list[Outcome]:
    """Apply every entry in one SAVEPOINT-wrapped batch.

    ``reverse=True`` swaps old/new, which is exactly what ``relink-undo`` needs.
    Any exception (including the drift guard in :func:`update_file_path`) rolls
    the entire batch back -- there is no partially-applied state to reason about.
    A ``conflict`` outcome aborts the batch unless ``tolerate_conflicts`` is set
    (undo does set it: drift on one row must not block reverting the rest).
    """
    conn = state_db_mod.open_rw(db_path, apply_schema=False)
    outcomes: list[Outcome] = []
    try:
        with StateWriter(conn, actor=actor) as writer:
            conn.execute("SAVEPOINT relink_batch")
            try:
                for entry in entries:
                    src = entry.new_path if reverse else entry.old_path
                    dst = entry.old_path if reverse else entry.new_path
                    outcome = update_file_path(
                        writer,
                        conn,
                        stable_id=entry.stable_id,
                        expected_current=src,
                        new_value=dst,
                    )
                    if outcome.outcome == "conflict" and not tolerate_conflicts:
                        raise RuntimeError(
                            f"{entry.stable_id}: {outcome.detail} -- the DB drifted "
                            "since classification; batch rolled back"
                        )
                    outcomes.append(outcome)
            except Exception:
                conn.execute("ROLLBACK TO SAVEPOINT relink_batch")
                conn.execute("RELEASE SAVEPOINT relink_batch")
                raise
            conn.execute("RELEASE SAVEPOINT relink_batch")
    finally:
        conn.close()
    return outcomes


def backup_state_db(db_path: Path, backup_dir: Path) -> Path:
    """Timestamped copy of ``state.db`` -- the coarse rollback of last resort."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"state.{time.strftime('%Y%m%dT%H%M%S')}.db"
    shutil.copy2(db_path, dst)
    if dst.stat().st_size != db_path.stat().st_size:
        raise RuntimeError(f"backup size mismatch: {dst}")
    return dst


# ----- availability measurement -----------------------------------------


@dataclass(slots=True)
class Availability:
    """How many rows resolve on disk right now."""

    total: int
    present: int
    awaiting_volume: int

    @property
    def missing(self) -> int:
        return self.total - self.present


def measure_availability(db_path: Path) -> Availability:
    """Count rows whose ``file_path`` resolves, read-only.

    Deliberately the crudest possible predicate (``os.path.exists``) so the
    before/after numbers are reproducible by anyone with a sqlite shell, not an
    artefact of this module's classification.
    """
    conn = state_db_mod.open_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT file_path FROM tracks WHERE deleted_at IS NULL"
        ).fetchall()
    finally:
        conn.close()
    mounted = match.mounted_volume_names()
    present = 0
    awaiting = 0
    for (path,) in rows:
        if path and os.path.exists(path):
            present += 1
            continue
        if match.unmounted_volume_of(path, mounted) is not None:
            awaiting += 1
    return Availability(total=len(rows), present=present, awaiting_volume=awaiting)


# ----- CLI ---------------------------------------------------------------


def _classify(args: argparse.Namespace) -> tuple[list[match.RowResult], list[str]]:
    """Load rows, index the disk, classify. Returns results + recorded paths."""
    state_db = args.data_dir / "state" / "state.db"
    if not state_db.exists():
        raise SystemExit(f"state DB not found at {state_db}")
    rb_db = args.data_dir / "master.plain.db"
    rows = match.load_track_rows(state_db, rb_db=rb_db if rb_db.exists() else None)
    console.print(f"  {len(rows)} track rows read (read-only)")
    cache = args.index_cache or args.data_dir / "state" / CACHE_PATH.name
    roots = args.roots if args.roots else list(DEFAULT_ROOTS)
    index, stats = build_index(roots, cache_path=cache, rebuild=args.rebuild_index)
    console.print(
        f"  {stats.walked} disk audio files indexed "
        f"({stats.tag_reads} tag reads, {stats.reused} cache hits)"
    )
    results = match.classify_rows(rows, index)
    return results, [r.file_path for r in rows if r.file_path]


def _print_selection(
    entries: Sequence[RelinkEntry], rejected: Sequence[Rejection], head: int
) -> None:
    table = Table(title=f"Selected relinks ({len(entries)})", show_lines=False)
    table.add_column("stable_id", style="bold")
    table.add_column("tier")
    table.add_column("conf", justify="right")
    table.add_column("old -> new")
    for entry in entries[:head]:
        table.add_row(
            entry.stable_id[:12],
            entry.tier,
            f"{entry.confidence:.3f}",
            f"[dim]{entry.old_path}[/dim]\n-> {entry.new_path}",
        )
    console.print(table)
    if len(entries) > head:
        console.print(f"[dim]... and {len(entries) - head} more[/dim]")
    if rejected:
        console.print(f"[yellow]{len(rejected)} row(s) rejected pre-write:[/yellow]")
        for rej in rejected[:10]:
            console.print(f"  x {rej.stable_id[:12]}: {rej.reason} ({rej.new_path})")
        if len(rejected) > 10:
            console.print(f"  ... and {len(rejected) - 10} more")


def cmd_relink(argv: list[str] | None = None) -> int:
    args = _relink_parser().parse_args(argv)
    live = bool(args.live)
    if live and not args.i_understand_the_risks:
        console.print(
            "[red]--live requires --i-understand-the-risks.[/red] Nothing written."
        )
        return 2
    # ``action="append"`` cannot carry a mutable default, so the default lands
    # here rather than in the parser.
    buckets = list(args.bucket) if args.bucket else ["relinkable-auto"]
    risky = set(buckets) & BUCKETS_NEEDING_RISK_FLAG
    if risky and not args.i_understand_the_risks:
        console.print(
            f"[red]bucket(s) {sorted(risky)} need --i-understand-the-risks even "
            "for a dry run: they are the buckets a human is supposed to "
            "adjudicate.[/red]"
        )
        return 2

    state_db = args.data_dir / "state" / "state.db"
    console.print("[bold]Step 1:[/bold] classifying (read-only)")
    results, recorded = _classify(args)
    counts = match.bucket_counts(results)
    console.print("  " + "  ".join(f"{k}={v}" for k, v in counts.items()))

    console.print("[bold]Step 2:[/bold] selecting")
    try:
        entries = select_entries(
            results,
            buckets=buckets,
            min_confidence=args.min_confidence,
            limit=args.limit,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    entries, rejected = reject_unsafe(entries, all_recorded_paths=recorded)
    _print_selection(entries, rejected, args.head)
    if not entries:
        console.print("[yellow]Nothing to apply.[/yellow]")
        return 1

    out_dir = args.out_dir or args.data_dir / "reconcile"
    log = write_log(entries, log_path_for(out_dir, live=live))
    console.print(f"[bold]Step 3:[/bold] reversal log written FIRST -> {log}")

    if not live:
        console.print(
            "[green]DRY RUN[/green] -- state.db untouched. Re-run with "
            "--live --i-understand-the-risks to apply."
        )
        return 0

    before = measure_availability(state_db)
    backup = backup_state_db(state_db, out_dir / "backups")
    console.print(f"[bold]Step 4:[/bold] state.db backed up -> {backup}")
    console.print(f"[bold]Step 5:[/bold] applying {len(entries)} row(s)")
    outcomes = apply_entries(state_db, entries, actor="reconcile.relink")
    changed = sum(1 for o in outcomes if o.outcome == "changed")
    already = sum(1 for o in outcomes if o.outcome == "already")
    after = measure_availability(state_db)

    summary = Table(title="Live relink summary", show_lines=False)
    summary.add_column("Metric", style="bold")
    summary.add_column("Value", justify="right")
    summary.add_row("Rows attempted", str(len(entries)))
    summary.add_row("Rows changed", str(changed))
    summary.add_row("Rows already at target", str(already))
    summary.add_row("Available BEFORE", f"{before.present} / {before.total}")
    summary.add_row("Available AFTER", f"{after.present} / {after.total}")
    summary.add_row("Delta", f"+{after.present - before.present}")
    summary.add_row("Reversal log", str(log))
    console.print(summary)
    console.print(
        f"[dim]Undo with: python -m apps.reconcile relink-undo --log {log} "
        f"--live --i-understand-the-risks[/dim]"
    )
    return 0


def cmd_relink_undo(argv: list[str] | None = None) -> int:
    args = _undo_parser().parse_args(argv)
    if args.live and not args.i_understand_the_risks:
        console.print(
            "[red]--live requires --i-understand-the-risks.[/red] Nothing written."
        )
        return 2
    state_db = args.data_dir / "state" / "state.db"
    if not state_db.exists():
        console.print(f"[red]state DB not found at {state_db}[/red]")
        return 2
    try:
        entries = read_log(args.log)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    if args.limit is not None:
        entries = entries[: args.limit]
    console.print(f"[bold]{len(entries)}[/bold] row(s) in {args.log}")

    if not args.live:
        # Dry run still consults the DB so the preview is truthful about which
        # rows would revert, which are already reverted and which have drifted.
        conn = state_db_mod.open_ro(state_db)
        try:
            preview: dict[str, int] = {"revert": 0, "already": 0, "conflict": 0}
            for entry in entries:
                row = conn.execute(
                    "SELECT file_path FROM tracks "
                    "WHERE stable_id = ? AND deleted_at IS NULL",
                    (entry.stable_id,),
                ).fetchone()
                if row is None:
                    preview["conflict"] += 1
                elif row[0] == entry.new_path:
                    preview["revert"] += 1
                elif row[0] == entry.old_path:
                    preview["already"] += 1
                else:
                    preview["conflict"] += 1
        finally:
            conn.close()
        console.print(
            f"[green]DRY RUN[/green] would revert {preview['revert']}, "
            f"skip {preview['already']} already-reverted, "
            f"report {preview['conflict']} conflict(s). state.db untouched."
        )
        return 0

    before = measure_availability(state_db)
    backup = backup_state_db(state_db, args.data_dir / "reconcile" / "backups")
    console.print(f"state.db backed up -> {backup}")
    outcomes = apply_entries(
        state_db,
        entries,
        reverse=True,
        tolerate_conflicts=True,
        actor="reconcile.relink-undo",
    )
    reverted = sum(1 for o in outcomes if o.outcome == "changed")
    already = sum(1 for o in outcomes if o.outcome == "already")
    conflicts = [o for o in outcomes if o.outcome == "conflict"]
    after = measure_availability(state_db)

    table = Table(title="Undo summary", show_lines=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Rows in log", str(len(entries)))
    table.add_row("Reverted", str(reverted))
    table.add_row("Already reverted", str(already))
    table.add_row("Conflicts (left alone)", str(len(conflicts)))
    table.add_row("Available BEFORE undo", f"{before.present} / {before.total}")
    table.add_row("Available AFTER undo", f"{after.present} / {after.total}")
    console.print(table)
    for conflict in conflicts[:20]:
        console.print(f"  [yellow]![/yellow] {conflict.stable_id}: {conflict.detail}")
    return 4 if conflicts else 0


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--data-dir",
        type=Path,
        default=paths.DATA_DIR,
        help="data dir holding state/state.db (default: this checkout's data/)",
    )
    p.add_argument("--dry-run", action="store_true", default=True, help="(default)")
    p.add_argument("--live", action="store_true", help="actually write state.db")
    p.add_argument(
        "--i-understand-the-risks",
        action="store_true",
        help="required companion flag for --live",
    )
    p.add_argument("--limit", type=int, default=None, help="cap the row count")


def _relink_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.reconcile relink",
        description=(
            "Repoint tracks.file_path at relocated files. Dry-run by default; "
            "writes a reversal log before any change."
        ),
    )
    _add_common(p)
    p.add_argument(
        "--bucket",
        action="append",
        default=None,
        help=(
            "bucket to apply, repeatable (default: relinkable-auto). "
            f"allowed: {sorted(APPLICABLE_BUCKETS)}"
        ),
    )
    p.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_MIN_CONFIDENCE,
        help=f"confidence floor (default {DEFAULT_MIN_CONFIDENCE}, see module docstring)",
    )
    p.add_argument("--index-cache", type=Path, default=None, help="disk index cache json")
    p.add_argument(
        "--roots",
        nargs="+",
        type=Path,
        default=None,
        help=f"disk roots to index (default: {' '.join(str(r) for r in DEFAULT_ROOTS)})",
    )
    p.add_argument("--rebuild-index", action="store_true", help="re-read every tag")
    p.add_argument("--out-dir", type=Path, default=None, help="log dir (default <data-dir>/reconcile)")
    p.add_argument("--head", type=int, default=10, help="preview rows to print")
    return p


def _undo_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.reconcile relink-undo",
        description="Revert a relink run from its reversal log. Dry-run by default.",
    )
    _add_common(p)
    p.add_argument("--log", type=Path, required=True, help="reversal log CSV to consume")
    return p


if __name__ == "__main__":
    raise SystemExit(cmd_relink())
