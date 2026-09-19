"""Audit: build RB <-> djay match CSV + summary.

Run from the project root with the venv active::

    python -m apps.audit.match_rb_djay [--no-fingerprint] [--out DIR]

Outputs (SYNC-02):

* ``data/sync/matches.csv`` -- schema contract consumed by Phase 3 playlist
  sync: ``rb_id,djay_uuid,confidence,signals_fired,status,rb_title,
  rb_artist,djay_title,djay_artist,rationale``.
* Rich summary table on stdout.
* ``data/sync/fingerprints.sqlite`` is created/refreshed on demand.

Read-only vs live DBs: this script never writes to Rekordbox or djay. It
calls :func:`apps.shared.paths.copy_live_dbs` to snapshot each live DB
into the working-copy location, then reads those copies.
"""
from __future__ import annotations

import argparse
import csv
from collections.abc import Iterable
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.shared import djay_db, paths, rekordbox_db
from apps.sync.fingerprint import FingerprintCache
from apps.sync.matcher import MatchResult, match_tracks

console = Console(width=120)


MATCHES_CSV_COLUMNS = (
    "rb_id",
    "djay_uuid",
    "confidence",
    "signals_fired",
    "status",
    "rb_title",
    "rb_artist",
    "djay_title",
    "djay_artist",
    "rationale",
)


def _load_rb_tracks() -> list:
    """Load Rekordbox tracks via the working-copy DB. Empty on failure."""
    try:
        db = rekordbox_db.open_db()
    except FileNotFoundError:
        console.print(
            "[yellow]rekordbox working DB missing and live DB not present; "
            "returning empty RB track list.[/yellow]"
        )
        return []
    try:
        return list(rekordbox_db.iter_tracks(db))
    finally:
        try:
            db.close()
        except Exception:
            pass


def _load_djay_tracks() -> list[djay_db.DjayTrack]:
    target = paths.DJAY_WORKING_DB
    if not target.exists():
        copied = paths.copy_live_dbs()
        target = copied.get("djay") or target
    if not target.exists():
        console.print(
            "[yellow]djay working DB missing; returning empty djay track list.[/yellow]"
        )
        return []
    return list(djay_db.iter_tracks(target))


def _write_matches_csv(
    result: MatchResult, out_path: Path
) -> int:
    """Write matched + review buckets to ``out_path``. Returns row count."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with out_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(MATCHES_CSV_COLUMNS)
        for pair in [*result.matched, *result.review]:
            writer.writerow(
                [
                    pair.rb_id,
                    pair.djay_uuid,
                    f"{pair.confidence:.4f}",
                    len(pair.signals),
                    pair.status,
                    pair.rb_title,
                    pair.rb_artist,
                    pair.djay_title,
                    pair.djay_artist,
                    pair.rationale,
                ]
            )
            rows += 1
    return rows


def _print_summary(result: MatchResult) -> None:
    table = Table(title="RB <-> djay match summary (SYNC-02)")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("RB total", str(result.stats.get("rb_total", 0)))
    table.add_row("djay total", str(result.stats.get("dj_total", 0)))
    table.add_row("auto-accept (matched)", str(len(result.matched)))
    table.add_row("human review", str(len(result.review)))
    table.add_row("rb-only", str(len(result.rb_only)))
    table.add_row("djay-only", str(len(result.djay_only)))
    rb_total = result.stats.get("rb_total", 0)
    if rb_total:
        coverage_pct = 100.0 * len(result.matched) / rb_total
        table.add_row("coverage (matched/RB)", f"{coverage_pct:.1f}%")
    console.print(table)


def run(
    *,
    out_dir: Path,
    use_fingerprint: bool,
    rb_tracks: Iterable | None = None,
    dj_tracks: Iterable | None = None,
    fingerprint_cache: FingerprintCache | None = None,
) -> MatchResult:
    """Core pipeline. Injectable inputs so tests can supply fixture data."""
    if rb_tracks is None:
        rb_list = _load_rb_tracks()
    else:
        rb_list = list(rb_tracks)

    if dj_tracks is None:
        dj_list = _load_djay_tracks()
    else:
        dj_list = list(dj_tracks)

    fingerprint_fn = None
    cache = fingerprint_cache
    close_cache_after = False
    if use_fingerprint:
        if cache is None:
            cache_path = out_dir / "fingerprints.sqlite"
            cache = FingerprintCache(cache_path)
            close_cache_after = True
        fingerprint_fn = cache.compare_pair_tracks

    try:
        result = match_tracks(rb_list, dj_list, fingerprint_fn=fingerprint_fn)
    finally:
        if close_cache_after and cache is not None:
            cache.close()

    out_path = out_dir / "matches.csv"
    rows = _write_matches_csv(result, out_path)
    console.print(f"[green]Wrote {rows} rows to {out_path}[/green]")
    _print_summary(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.audit.match_rb_djay",
        description="Produce the RB <-> djay track match CSV.",
    )
    parser.add_argument(
        "--no-fingerprint",
        action="store_true",
        help="Disable the lazy chromaprint signal (5-signal mode).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=paths.DATA_DIR / "sync",
        help="Output directory (default: data/sync).",
    )
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    run(out_dir=args.out, use_fingerprint=not args.no_fingerprint)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
