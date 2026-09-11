"""``promote`` subcommand of :mod:`apps.mik.cli`, split into its own module
to keep ``cli.py`` under the file-size review threshold
(``scripts/quality_gate.py``). Purely mechanical: ``main()`` in ``cli.py``
still dispatches to ``cmd_promote`` exactly as before.
"""
from __future__ import annotations

import argparse
import sqlite3
from typing import Any

from apps.shared.equivalence import EquivalenceGate
from apps.shared.state import db as state_db

from . import load as loader
from . import promote as promoter
from .cli_common import _emit, _gate, _state_db_path

# ----------------------------------------------------------------- promote


def _promote_assignments(
    args: argparse.Namespace, conn: sqlite3.Connection
) -> dict[str, str]:
    """Resolves --discover vs an explicit pair, split out of ``cmd_promote``
    to keep it under the complexity ceiling."""
    if args.discover:
        return promoter.discover(conn, allow_fuzzy=args.allow_fuzzy)
    if args.source_row_id and args.stable_id:
        return {args.source_row_id: args.stable_id}
    raise SystemExit(
        "promote needs either --discover or both --source-row-id and "
        "--stable-id"
    )


def _promote_live_results(
    args: argparse.Namespace,
    conn: sqlite3.Connection,
    gate: EquivalenceGate,
    assignments: dict[str, str],
) -> dict[str, Any] | None:
    """The --live outcome summary, split out of ``cmd_promote`` to keep it
    under the complexity ceiling."""
    if not (args.live and assignments):
        return None
    outcomes = promoter.promote_all(
        conn,
        gate,
        assignments,
        overwrite_lower_precedence=args.overwrite_lower_precedence,
    )
    return {
        "rows": len(outcomes),
        "fields_written": sum(o.fields_written for o in outcomes),
        "segment_rows": sum(o.segment_rows for o in outcomes),
        "already_promoted": sum(o.already_promoted for o in outcomes),
        "blocked_by_gate": sorted(
            {name for o in outcomes for name in o.blocked_by_gate}
        ),
    }


def cmd_promote(args: argparse.Namespace) -> int:
    path = _state_db_path(args)
    # Same dry-run contract as cmd_availability: open_rw migrates on open,
    # so only a --live invocation may open the state DB writable, and the
    # dry-run branch uses open_dry_run (a migrated disposable sibling-file copy) rather
    # than open_ro so previewing a promotion against a v8-only table
    # (unmatched_source_analysis) does not require the real DB pre-migrated.
    conn = state_db.open_rw(path) if args.live else state_db.open_dry_run(path)
    gate = _gate(args)
    try:
        assignments = _promote_assignments(args, conn)
        results = _promote_live_results(args, conn, gate, assignments)
        payload: dict[str, Any] = {
            "mode": "live" if args.live else "dry-run",
            "state_db": str(path),
            "pending_by_reason": promoter.pending_counts(conn),
            "assignments": len(assignments),
            "equivalence": gate.summary(list(loader.GATED_FIELDS)),
            "applied": results,
        }
    finally:
        conn.close()
    _emit(
        payload,
        as_json=args.json,
        lines=[
            f"[{payload['mode']}] promotion against {path}",
            (
                "  pending staged source rows: "
                + ", ".join(
                    f"{k}={v}"
                    for k, v in sorted(payload["pending_by_reason"].items())
                )
            ),
            f"  promotable now: {payload['assignments']}",
            f"  applied: {results}"
            if results is not None
            else "  (dry-run: pass --live to write)",
        ],
    )
    return 0


