"""Repo session helpers for pairings CLI entry points (not public API)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from apps.shared.pairings import PairingsError, PairingsRepo

from ._common import build_repo


def pairings_error_exit(exc: PairingsError) -> int:
    print(f"error: {exc}", file=sys.stderr)
    return 2


@contextmanager
def pairing_repo(db_path: Path | None) -> Iterator[PairingsRepo]:
    repo, conn = build_repo(db_path)
    try:
        yield repo
    finally:
        conn.close()
