"""Shared argparse shape for state DB normalize / repair CLIs."""

from __future__ import annotations

import argparse
from pathlib import Path


def build_data_dir_dry_live_parser(*, prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument(
        "--data-dir",
        required=True,
        type=Path,
        help="data dir holding state/state.db",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="report repairs, write nothing")
    mode.add_argument("--live", action="store_true", help="apply the repairs")
    return parser
