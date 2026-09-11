"""CLI parity for the library-wheel HTTP read model (AGENTS.md agent-native parity)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from apps.shared.paths import REKORDBOX_PLAIN_DB, STATE_DB

from .query import AXES, LibraryWheelError, query_library_wheel


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m apps.library_wheel")
    parser.add_argument("--state-db", type=Path, default=STATE_DB)
    parser.add_argument("--master-db", type=Path, default=REKORDBOX_PLAIN_DB)
    parser.add_argument(
        "--axis",
        choices=[a.key for a in AXES],
        default="play_count",
        help="per-track overlay value; a disabled axis returns the real "
        "genre tree with axis_value null and a reason, never a fake number",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        payload = query_library_wheel(args.state_db, args.master_db, axis=args.axis)
    except (LibraryWheelError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


__all__ = ["main"]
