"""Phase 4 unified sync-diff CSV emitter."""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from apps.shared import paths
from apps.shared.normalised import NormalisedAnalysis
from apps.sync.conflict import resolve_conflict


@dataclass(slots=True, frozen=True)
class DiffRow:
    rb_content_id: str
    djay_uuid: str
    field: str
    rb_value: str
    djay_value: str
    resolution: str
    action_hint: str


_ACTION_HINTS = {
    "accept_rb": "write RB -> djay",
    "accept_djay": "write djay -> RB",
    "conflict": "manual review",
    "no_change": "skip",
}


def iter_match_pairs(
    matches_csv: Path, *, min_confidence: float = 0.70
) -> Iterator[tuple[str, str]]:
    with matches_csv.open("r", newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for rec in reader:
            try:
                conf = float(rec.get("confidence", "0") or 0)
            except ValueError:
                conf = 0.0
            if conf < min_confidence:
                continue
            rb_id = rec.get("rb_content_id") or rec.get("rb_id") or ""
            djay_uuid = rec.get("djay_uuid") or ""
            if rb_id and djay_uuid:
                yield rb_id, djay_uuid


def build_analysis_diff(
    rb_analysis_by_id: dict[str, NormalisedAnalysis],
    djay_analysis_by_uuid: dict[str, NormalisedAnalysis],
    rb_rating_by_id: dict[str, int],
    djay_rating_by_uuid: dict[str, int],
    pairs: Iterable[tuple[str, str]],
    *,
    prefer: str = "newest",
) -> tuple[list[DiffRow], list[DiffRow]]:
    analysis_rows: list[DiffRow] = []
    rating_rows: list[DiffRow] = []
    for rb_id, djay_uuid in pairs:
        rb_a = rb_analysis_by_id.get(rb_id)
        djay_a = djay_analysis_by_uuid.get(djay_uuid)
        rb_r = rb_rating_by_id.get(rb_id)
        djay_r = djay_rating_by_uuid.get(djay_uuid)
        rb_mtime = getattr(rb_a, "modified_at", None) if rb_a else None
        djay_mtime = getattr(djay_a, "modified_at", None) if djay_a else None
        if rb_r is not None or djay_r is not None:
            # Codex P04-01: pass per-side modified timestamps so
            # ``prefer="newest"`` actually uses newest-wins instead of
            # silently collapsing to RB-default.
            res = resolve_conflict(
                "rating",
                rb_r or 0,
                djay_r or 0,
                rb_mtime,
                djay_mtime,
                prefer=prefer,
            )
            rating_rows.append(
                DiffRow(
                    rb_content_id=rb_id,
                    djay_uuid=djay_uuid,
                    field="rating",
                    rb_value=str(rb_r) if rb_r is not None else "",
                    djay_value=str(djay_r) if djay_r is not None else "",
                    resolution=res,
                    action_hint=_ACTION_HINTS[res],
                )
            )
        if rb_a is None and djay_a is None:
            continue
        for field in ("bpm", "manual_bpm", "key_camelot", "energy", "tags"):
            rb_v = getattr(rb_a, field, None) if rb_a else None
            djay_v = getattr(djay_a, field, None) if djay_a else None
            if rb_v is None and djay_v is None:
                continue
            # Codex P04-01: per-side timestamps must reach the resolver
            # or ``--prefer newest`` collapses to RB-default.
            res = resolve_conflict(
                field if field != "manual_bpm" else "manual_bpm",
                rb_v,
                djay_v,
                rb_mtime,
                djay_mtime,
                prefer=prefer,
            )
            analysis_rows.append(
                DiffRow(
                    rb_content_id=rb_id,
                    djay_uuid=djay_uuid,
                    field=field,
                    rb_value="" if rb_v is None else str(rb_v),
                    djay_value="" if djay_v is None else str(djay_v),
                    resolution=res,
                    action_hint=_ACTION_HINTS[res],
                )
            )
    return analysis_rows, rating_rows


def write_diff_csv(rows: Iterable[DiffRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            [
                "rb_content_id",
                "djay_uuid",
                "field",
                "rb_value",
                "djay_value",
                "resolution",
                "action_hint",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r.rb_content_id,
                    r.djay_uuid,
                    r.field,
                    r.rb_value,
                    r.djay_value,
                    r.resolution,
                    r.action_hint,
                ]
            )


def summarise(rows: Iterable[DiffRow]) -> dict[str, dict[str, int]]:
    out: dict[str, Counter] = {}
    for r in rows:
        out.setdefault(r.field, Counter())[r.resolution] += 1
    return {k: dict(v) for k, v in out.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apps.audit.sync_diff")
    parser.add_argument("--matches", type=Path, default=paths.DATA_DIR / "sync" / "matches.csv")
    parser.add_argument("--out-dir", type=Path, default=paths.DATA_DIR / "sync")
    parser.add_argument("--min-confidence", type=float, default=0.70)
    parser.add_argument("--prefer", choices=["rb", "djay", "newest"], default="newest")
    parser.add_argument("--include-cues", action="store_true")
    args = parser.parse_args(argv)

    if not args.matches.exists():
        print(f"[sync_diff] matches.csv missing: {args.matches}", file=sys.stderr)
        return 2

    from apps.shared.djay_db import iter_analysis as djay_iter_analysis
    from apps.shared.djay_db import iter_tracks as djay_iter_tracks
    from apps.shared.rekordbox_db import iter_analysis as rb_iter_analysis
    from apps.shared.rekordbox_db import iter_tracks as rb_iter_tracks
    from apps.shared.rekordbox_db import open_db

    rb_db = open_db()
    try:
        rb_analysis = {a.uuid_or_id: a for a in rb_iter_analysis(rb_db)}
        rb_ratings = {str(t.id): (t.rating or 0) for t in rb_iter_tracks(rb_db)}
    finally:
        try:
            rb_db.close()
        except Exception:
            pass

    djay_analysis = {a.uuid_or_id: a for a in djay_iter_analysis(paths.DJAY_WORKING_DB)}
    djay_ratings = {t.uuid: (t.rating or 0) for t in djay_iter_tracks(paths.DJAY_WORKING_DB)}

    pairs = list(iter_match_pairs(args.matches, min_confidence=args.min_confidence))
    analysis_rows, rating_rows = build_analysis_diff(
        rb_analysis, djay_analysis, rb_ratings, djay_ratings, pairs, prefer=args.prefer
    )
    write_diff_csv(analysis_rows, args.out_dir / "analysis-diff.csv")
    write_diff_csv(rating_rows, args.out_dir / "ratings-diff.csv")
    print(
        f"[sync_diff] wrote {len(analysis_rows)} analysis rows + "
        f"{len(rating_rows)} rating rows to {args.out_dir}"
    )
    if args.include_cues:
        from apps.audit.cue_comparison import main as cue_main

        cue_main(
            [
                "--matches", str(args.matches),
                "--out", str(args.out_dir / "cue-diff.csv"),
                "--min-confidence", str(args.min_confidence),
            ]
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DiffRow",
    "iter_match_pairs",
    "build_analysis_diff",
    "write_diff_csv",
    "summarise",
    "main",
]
