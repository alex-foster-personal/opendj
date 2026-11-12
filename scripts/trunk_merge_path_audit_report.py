"""Part of `trunk_merge_path_audit.py`, split out with no behavior change: report."""

from __future__ import annotations

import argparse
import json

from scripts.trunk_merge_path_audit_evidence import (
    CUTOVER_SHA,
    DEFAULT_REPOSITORY,
)


def _unknown_output(args: argparse.Namespace, message: str) -> str:
    if args.json:
        if args.rev_range:
            selection = args.rev_range
        elif args.since or args.until:
            selection = f"{args.ref} {args.since or '?'}..{args.until or '?'}"
        else:
            selection = args.ref
        return json.dumps(
            {
                "schema_version": 1,
                "repository": DEFAULT_REPOSITORY,
                "cutover_sha": CUTOVER_SHA,
                "selection": selection,
                "evidence_source": "git first-parent history + GitHub GraphQL/REST",
                "summary": {
                    "commits": 0,
                    "tested": 0,
                    "untested": 0,
                    "unknown": 1,
                    "tested_coverage_pct": None,
                },
                "error": message,
            },
            indent=2,
            sort_keys=True,
        )
    return f"[trunk-merge-path-audit] UNKNOWN: {message}"
