"""Phase 4 SYNC-04 bulk cue sync CLI.

Dry-run default. Live modes require ``--live --cautious --tracks ...``
(5-10 track cautious pass) or ``--live --bulk`` (paired with
``--i-understand-the-risks``; optionally narrowed by ``--tracks``).

Prior to v1.0 adversarial review fix #4, ``--bulk`` without ``--tracks``
was erroneously gated -- that gate has been removed because it blocked
the legitimate bulk-all-tracks workflow and the real safety rail is
``--i-understand-the-risks`` (enforced in main()).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from apps.shared import paths
from apps.shared.rekordbox_writeback import require_writeback_enabled
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
    djay_db_path: Path = paths.DJAY_WORKING_DB,  # noqa: ARG001 - keyword contract
) -> int:
    """STUB live-run. Phase 4 Plan 3 ships the safety scaffold; the full
    per-track writer plumbing (RB + djay) is exercised in the smoke test.
    Live bulk-write against a real user library is an O2 probe item that
    requires manual sign-off and is intentionally deferred from this CLI
    to the runbook ``docs/phase-04-probe-o2-runbook.md``.

    The ``--live`` path does NOT mutate cue bytes. It exits 0 after
    exercising the 7-rail safety harness and emits a stderr banner so
    operators cannot mistake it for a real write. See ``_print_stub_banner``.
    """
    if not cautious and not bulk:
        print("[apply_cues] need --cautious or --bulk with --live", file=sys.stderr)
        return 2
    # v1.0 adversarial review (#4, HIGH): the previous gate refused
    # ``--bulk`` whenever ``only_tracks is None``, which blocked the
    # legitimate bulk-all-tracks workflow (``--bulk`` paired with
    # ``--i-understand-the-risks`` and NO ``--tracks``). Mirrors
    # ``apply_ratings``: the ``--i-understand-the-risks`` requirement is
    # enforced in ``main`` before reaching here, so no extra gate is
    # needed. Callers can still narrow with ``--tracks`` if desired.

    # [I1 fix] Loud banner so users cannot mistake this CLI path for a real
    # write. The actual rb_writer.write_cues / djay_writer.patch_cue_points
    # plumbing lives in the smoke-test harness; the probe runbook
    # (docs/phase-04-probe-o2-runbook.md) is the sanctioned live-write path.
    _print_stub_banner()

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
        if not rows:
            print(
                "[apply_cues] no rows matched --tracks filter; "
                "nothing to do.",
                file=sys.stderr,
            )
            return 0

    # Rail 4: post-write verifier for the stub cue sync. Since the stub
    # does not mutate cue bytes, the verifier is a pure shape-check -- it
    # exercises the session's verify plumbing so the rail is present and
    # observable even before live cue writes are wired up.
    def _stub_verifier(_tid: str, _unused: object = None) -> bool:
        return True

    require_writeback_enabled("module.sync.apply_cues")
    with LiveWriteSession(
        target="rekordbox",
        reason="SYNC-04 cue sync (RB side)",
        flag_ok=flag_ok,
        db_path=rb_db_path,
        verifier=_stub_verifier,
    ) as sess:
        for row in rows:
            tid = row.get("rb_content_id") or row.get("djay_uuid") or "?"
            with sess.per_track(tid) as w:
                # Plan 3 minimal: record the intent, append reverse.
                # The actual per-cue insert/update is the smoke-test path
                # (test_phase4_smoke) once the user has run a cautious pass.
                w.write({"rb_only": row.get("rb_only_positions", "")})
                if w.verify_readback():
                    w.append_reverse(
                        f"# revert RB cues for "
                        f"content_id={row.get('rb_content_id')}"
                    )
    print(
        f"[apply_cues] cautious pass complete on {len(rows)} rows "
        "(STUB -- no cue bytes were written; see banner above)."
    )
    return 0


def _print_stub_banner() -> None:
    """Print the mandatory stub-mode banner to stderr.

    Kept in a dedicated helper so tests can import and assert on its exact
    text, and so future live wiring can remove one call site rather than
    scan for a banner string.
    """
    print(
        "\n"
        "============================================================\n"
        "  [apply_cues] WARNING -- STUB LIVE MODE, NO REAL CUE WRITES\n"
        "  The --live path exercises the 7-rail safety harness only.\n"
        "  No cue bytes are written to the rekordbox or djay DB.\n"
        "  Real cue writes are gated behind the Phase 4 O2 probe\n"
        "  runbook: docs/phase-04-probe-o2-runbook.md\n"
        "============================================================\n",
        file=sys.stderr,
    )


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
