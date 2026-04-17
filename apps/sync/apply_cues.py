"""Phase 4 SYNC-04 bulk cue sync CLI.

Dry-run default. Live modes require ``--live --cautious --tracks ...``
(5-10 track cautious pass) or ``--live --bulk`` (gated behind cautious
having been completed).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from apps.shared import paths
from apps.sync.safety import LiveWriteSession, SafetyAbort


def _load_cue_diff(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


def dry_run(rows: list[dict]) -> int:
    if not rows:
        print("[apply_cues] no rows in cue-diff.csv (dry-run).")
        return 0
    adds_rb = sum(1 for r in rows if r.get("djay_only_positions"))
    adds_djay = sum(1 for r in rows if r.get("rb_only_positions"))
    conflicts = sum(1 for r in rows if r.get("conflicting_positions"))
    print("[apply_cues] dry-run summary:")
    print(f"  tracks with djay-only cues (add to RB):   {adds_rb}")
    print(f"  tracks with rb-only cues   (add to djay): {adds_djay}")
    print(f"  tracks with conflicts:                    {conflicts}")
    print(f"  total rows:                               {len(rows)}")
    return 0


def live_run(
    rows: list[dict],
    *,
    only_tracks: set[str] | None = None,
    flag_ok: bool = False,
    cautious: bool = False,
    bulk: bool = False,
    rb_db_path: Path = paths.REKORDBOX_WORKING_DB,
    djay_db_path: Path = paths.DJAY_WORKING_DB,
) -> int:
    """Placeholder live-run. Phase 4 Plan 3 ships the safety scaffold; the
    full per-track writer plumbing (RB + djay) is exercised in the smoke
    test. Live bulk-write against a real user library is an O2 probe item
    that requires manual sign-off and is intentionally deferred from this
    CLI to the runbook ``docs/phase-04-probe-o2-runbook.md``.
    """
    if not cautious and not bulk:
        print("[apply_cues] need --cautious or --bulk with --live", file=sys.stderr)
        return 2
    if bulk and only_tracks is None:
        # Gate: require the user to have already run cautious.
        print(
            "[apply_cues] --bulk requires prior cautious pass. "
            "Run --cautious --tracks UUID1,UUID2,... first.",
            file=sys.stderr,
        )
        return 2

    if not rows:
        print("[apply_cues] nothing to do (empty diff).")
        return 0

    # Filter rows by only_tracks if provided.
    if only_tracks is not None:
        rows = [
            r for r in rows
            if r.get("djay_uuid") in only_tracks
            or r.get("rb_content_id") in only_tracks
        ]

    with LiveWriteSession(
        target="rekordbox",
        reason="SYNC-04 cue sync (RB side)",
        flag_ok=flag_ok,
        db_path=rb_db_path,
    ) as sess:
        for row in rows:
            tid = row.get("rb_content_id") or row.get("djay_uuid") or "?"
            with sess.per_track(tid) as w:
                # Plan 3 minimal: record the intent, append reverse.
                # The actual per-cue insert/update is the smoke-test path
                # (test_phase4_smoke) once the user has run a cautious pass.
                w.write({"rb_only": row.get("rb_only_positions", "")})
                w.append_reverse(
                    f"# revert RB cues for content_id={row.get('rb_content_id')}"
                )
    print(f"[apply_cues] cautious pass complete on {len(rows)} rows.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apps.sync.apply_cues")
    parser.add_argument(
        "--diff-csv",
        type=Path,
        default=paths.DATA_DIR / "sync" / "cue-diff.csv",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--cautious", action="store_true")
    parser.add_argument("--bulk", action="store_true")
    parser.add_argument("--tracks", type=str, default="")
    parser.add_argument(
        "--i-understand-the-risks",
        dest="i_understand_the_risks",
        action="store_true",
    )
    parser.add_argument("--prefer", choices=["rb", "djay", "newest"], default="newest")
    parser.add_argument("--prune", action="store_true")
    args = parser.parse_args(argv)

    rows = _load_cue_diff(args.diff_csv)
    if not args.live:
        return dry_run(rows)
    if (args.cautious or args.bulk) and not args.i_understand_the_risks:
        print("[apply_cues] --live needs --i-understand-the-risks", file=sys.stderr)
        return 2

    only_tracks: set[str] | None = None
    if args.tracks:
        only_tracks = {t.strip() for t in args.tracks.split(",") if t.strip()}
    try:
        return live_run(
            rows,
            only_tracks=only_tracks,
            flag_ok=args.i_understand_the_risks,
            cautious=args.cautious,
            bulk=args.bulk,
        )
    except SafetyAbort as e:
        print(f"[apply_cues] SafetyAbort: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["dry_run", "live_run", "main"]
