"""Operator CLI for the library-integrity guard, plus the historic import path.

The guard itself moved to :mod:`apps.shared.library_integrity` on Mon 17 Aug
2026. It is a gate, not an audit report: ``apps.sync.playlist_apply`` calls it
before every live write (GUARD-lineage H1), and with the guard owned by
``apps.audit`` that made ``sync`` import ``audit`` while ``audit`` already
imported ``sync`` in three places -- a package cycle. ``apps.shared`` imports
nothing from its sibling packages (enforced by the ``shared-is-the-stable-core``
contract in ``.importlinter``), so hosting the guard there inverts the
dependency instead of papering over it.

What stays here:

R1. The ``python -m apps.audit.library_integrity [--threshold N] [--strict]``
    CLI, which is documented in .planning/ROADMAP.md and the usb-import-export
    skill. Presentation only: it formats a report it did not compute.
      [if `python -m apps.audit.library_integrity --help` errors then ⛔️]

R2. A re-export of the gate-facing surface so the historic import path keeps
    working for callers and docs that already reference it.
      [if `from apps.audit.library_integrity import assert_healthy` resolves to
       a different object than the shared one then ⛔️]

-Claude
"""
from __future__ import annotations

import argparse

from apps.shared.library_integrity import (
    DEFAULT_THRESHOLD,
    IntegrityReport,
    LibraryIntegrityError,
    TrackLike,
    assert_healthy,
    check_integrity,
    live_report,
    rehome_path,
)

__all__ = [
    "DEFAULT_THRESHOLD",
    "IntegrityReport",
    "LibraryIntegrityError",
    "TrackLike",
    "assert_healthy",
    "check_integrity",
    "live_report",
    "main",
    "rehome_path",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.audit.library_integrity",
        description="Check that Rekordbox tracks resolve to files on disk.",
    )
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD,
        help=f"max acceptable broken_ratio (default {DEFAULT_THRESHOLD}).",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="exit non-zero when broken_ratio exceeds --threshold.",
    )
    args = parser.parse_args(argv)

    rep = live_report()
    print(
        f"tracks={rep.total} streaming={rep.streaming} with_path={rep.with_path}\n"
        f"  present={rep.present} ({rep.present / max(rep.with_path,1):.1%})\n"
        f"  rehomable={rep.rehomable} ({rep.rehomable_ratio:.1%})  "
        f"<- fixable by re-homing to $HOME\n"
        f"  missing={rep.missing} ({rep.missing_ratio:.1%})  "
        f"<- bytes not on this machine\n"
        f"  broken_ratio={rep.broken_ratio:.1%}  threshold={args.threshold:.1%}"
    )
    if rep.stale_home_prefixes:
        print("  stale home prefixes:")
        for p, n in sorted(rep.stale_home_prefixes.items(), key=lambda kv: -kv[1])[:5]:
            print(f"    {n:6d}  {p}")
    if args.strict:
        try:
            assert_healthy(rep, threshold=args.threshold)
        except LibraryIntegrityError as exc:
            print(f"\nFAIL: {exc}")
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
