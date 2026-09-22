"""Phase 4 audit: per-track RB vs djay cue comparison.

Reads ``data/sync/matches.csv`` (from Phase 2's matcher), loads cues from
both sides, and emits ``data/sync/cue-diff.csv`` with per-track summary
columns. Position tolerance: ±20 ms per D6.

Run via ``python -m apps.audit.cue_comparison``.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from apps.shared import paths
from apps.shared.normalised import NormalisedCue

TOLERANCE_MSEC = 20


@dataclass(slots=True, frozen=True)
class CueDiffRow:
    rb_content_id: str
    djay_uuid: str
    rb_cue_count: int
    djay_cue_count: int
    rb_only_positions: tuple[int, ...]
    djay_only_positions: tuple[int, ...]
    conflicting_positions: tuple[int, ...]
    union_count: int


def _positions_match(a: NormalisedCue, b: NormalisedCue, tol_ms: int) -> bool:
    if a.kind != b.kind:
        return False
    if abs(a.position_msec - b.position_msec) > tol_ms:
        return False
    if a.kind == "hot" and a.index is not None and b.index is not None:
        return a.index == b.index
    return True


def compare_cue_lists(
    rb_cues: Iterable[NormalisedCue],
    djay_cues: Iterable[NormalisedCue],
    *,
    tolerance_msec: int = TOLERANCE_MSEC,
    check_metadata: bool = True,
) -> tuple[list[int], list[int], list[int]]:
    """Return ``(rb_only, djay_only, conflicts)`` as position-ms lists.

    A conflict is a matched pair (within ``tolerance_msec``) where either
    side has a different name, colour, or loop length. When
    ``check_metadata`` is false, matches use kind, position, and hot-cue index
    only (ANLZ PCOB/PCO2 siblings may carry names PCOB omits).
    """
    rb_list = list(rb_cues)
    djay_list = list(djay_cues)
    rb_matched = set()
    djay_matched = set()
    conflicts: list[int] = []
    for i, a in enumerate(rb_list):
        for j, b in enumerate(djay_list):
            if j in djay_matched:
                continue
            if _positions_match(a, b, tolerance_msec):
                rb_matched.add(i)
                djay_matched.add(j)
                if check_metadata and (
                    (a.name or "") != (b.name or "")
                    or a.color_rgb != b.color_rgb
                    or (a.loop_length_msec or 0) != (b.loop_length_msec or 0)
                ):
                    conflicts.append(a.position_msec)
                break
    rb_only = [c.position_msec for i, c in enumerate(rb_list) if i not in rb_matched]
    djay_only = [
        c.position_msec for j, c in enumerate(djay_list) if j not in djay_matched
    ]
    return rb_only, djay_only, conflicts


def build_diff_rows(
    matches_csv: Path,
    rb_db,  # pyrekordbox Rekordbox6Database
    djay_db_path: Path,
    *,
    min_confidence: float = 0.70,
) -> list[CueDiffRow]:
    """Walk matches.csv and produce per-track diff rows."""
    from apps.shared.djay_db import iter_cues as iter_djay_cues
    from apps.shared.rekordbox_db import iter_cues as iter_rb_cues

    rows: list[CueDiffRow] = []
    with matches_csv.open("r", newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for rec in reader:
            try:
                confidence = float(rec.get("confidence", "0") or 0)
            except ValueError:
                confidence = 0.0
            if confidence < min_confidence:
                continue
            rb_id = rec.get("rb_content_id") or rec.get("rb_id") or ""
            djay_uuid = rec.get("djay_uuid") or ""
            if not rb_id or not djay_uuid:
                continue
            rb_cues = iter_rb_cues(rb_db, rb_id)
            djay_cues = iter_djay_cues(djay_db_path, djay_uuid)
            rb_only, djay_only, conflicts = compare_cue_lists(rb_cues, djay_cues)
            union_count = len(rb_cues) + len(djay_only)
            rows.append(
                CueDiffRow(
                    rb_content_id=rb_id,
                    djay_uuid=djay_uuid,
                    rb_cue_count=len(rb_cues),
                    djay_cue_count=len(djay_cues),
                    rb_only_positions=tuple(rb_only),
                    djay_only_positions=tuple(djay_only),
                    conflicting_positions=tuple(conflicts),
                    union_count=union_count,
                )
            )
    return rows


def write_diff_csv(rows: Iterable[CueDiffRow], out_path: Path) -> None:
    """Emit ``cue-diff.csv``."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            [
                "rb_content_id",
                "djay_uuid",
                "rb_cue_count",
                "djay_cue_count",
                "rb_only_positions",
                "djay_only_positions",
                "conflicting_positions",
                "union_count",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r.rb_content_id,
                    r.djay_uuid,
                    r.rb_cue_count,
                    r.djay_cue_count,
                    ";".join(str(p) for p in r.rb_only_positions),
                    ";".join(str(p) for p in r.djay_only_positions),
                    ";".join(str(p) for p in r.conflicting_positions),
                    r.union_count,
                ]
            )


def print_summary(rows: list[CueDiffRow]) -> None:
    """Rich-styled summary table (falls back to plain text if rich absent)."""
    try:
        from rich.console import Console
        from rich.table import Table
    except Exception:  # pragma: no cover
        for r in rows[:20]:
            print(r)
        return

    console = Console(width=120)
    total_rb = sum(r.rb_cue_count for r in rows)
    total_djay = sum(r.djay_cue_count for r in rows)
    total_conflicts = sum(len(r.conflicting_positions) for r in rows)
    total_rb_only = sum(len(r.rb_only_positions) for r in rows)
    total_djay_only = sum(len(r.djay_only_positions) for r in rows)

    t = Table(title="Phase 4 cue comparison summary")
    t.add_column("metric")
    t.add_column("value", justify="right")
    t.add_row("matched tracks", str(len(rows)))
    t.add_row("total RB cues", str(total_rb))
    t.add_row("total djay cues", str(total_djay))
    t.add_row("rb-only cue positions", str(total_rb_only))
    t.add_row("djay-only cue positions", str(total_djay_only))
    t.add_row("conflicts", str(total_conflicts))
    console.print(t)

    top = sorted(rows, key=lambda r: -len(r.conflicting_positions))[:10]
    if top and any(r.conflicting_positions for r in top):
        t2 = Table(title="Top 10 tracks by conflict count")
        t2.add_column("rb_content_id")
        t2.add_column("djay_uuid")
        t2.add_column("conflicts", justify="right")
        for r in top:
            if not r.conflicting_positions:
                continue
            t2.add_row(r.rb_content_id, r.djay_uuid, str(len(r.conflicting_positions)))
        console.print(t2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apps.audit.cue_comparison",
        description="Compare cues between Rekordbox and djay for matched pairs.",
    )
    parser.add_argument(
        "--matches",
        type=Path,
        default=paths.DATA_DIR / "sync" / "matches.csv",
        help="Path to matches.csv produced by apps.audit.match_rb_djay.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=paths.DATA_DIR / "sync" / "cue-diff.csv",
        help="Path for the cue-diff.csv output.",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.70,
        help="Confidence threshold for matches.csv rows (default 0.70).",
    )
    args = parser.parse_args(argv)

    if not args.matches.exists():
        print(f"[cue_comparison] matches csv missing: {args.matches}", file=sys.stderr)
        return 2

    from apps.shared.rekordbox_db import open_db

    rb_db = open_db()
    try:
        rows = build_diff_rows(
            args.matches,
            rb_db,
            paths.DJAY_WORKING_DB,
            min_confidence=args.min_confidence,
        )
    finally:
        try:
            rb_db.close()
        except Exception:
            pass

    write_diff_csv(rows, args.out)
    print_summary(rows)
    print(f"[cue_comparison] wrote {len(rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
