"""Shared helpers for the pairings CLI entry points.

Isolates repo construction so the three entrypoints (``add`` / ``remove`` /
``list``) stay thin. SQLite path resolution lives in
``apps.shared.state.phase08_open``.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from apps.shared.pairings import PairingsRepo
from apps.shared.pairings.models import DIRECTIONS, SOURCES
from apps.shared.state.phase08_open import open_phase08_state_db


def build_repo(path: Path | None = None) -> tuple[PairingsRepo, sqlite3.Connection]:
    conn = open_phase08_state_db(path)
    return PairingsRepo(conn, ensure_schema=False), conn


def add_db_argument(parser: argparse.ArgumentParser) -> None:
    """Register the shared ``--db`` override used by all pairings CLIs."""
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="override state DB path (default: apps.shared.paths.STATE_DB)",
    )


def add_from_to_arguments(
    parser: argparse.ArgumentParser,
    *,
    required: bool = True,
    from_help: str | None = None,
    to_help: str | None = None,
) -> None:
    """Register ``--from`` / ``--to`` stable_id flags shared by add and remove."""
    from_kwargs: dict[str, object] = {"dest": "from_id", "required": required}
    to_kwargs: dict[str, object] = {"dest": "to_id", "required": required}
    if from_help is not None:
        from_kwargs["help"] = from_help
    if to_help is not None:
        to_kwargs["help"] = to_help
    parser.add_argument("--from", **from_kwargs)
    parser.add_argument("--to", **to_kwargs)


def add_from_filter_argument(
    parser: argparse.ArgumentParser,
    *,
    help: str,
) -> None:
    """Register optional ``--from`` stable_id filter (``list``)."""
    parser.add_argument("--from", dest="from_id", default=None, help=help)


def add_direction_argument(
    parser: argparse.ArgumentParser,
    *,
    required: bool = False,
    default: str = "either",
    help: str | None = None,
) -> None:
    """Register ``--direction`` with shared ``DIRECTIONS`` choices."""
    kwargs: dict[str, object] = {"choices": list(DIRECTIONS), "required": required}
    if not required:
        kwargs["default"] = default
    if help is not None:
        kwargs["help"] = help
    parser.add_argument("--direction", **kwargs)


def add_source_argument(
    parser: argparse.ArgumentParser,
    *,
    default: str | None = "manual",
    help: str | None = None,
) -> None:
    """Register ``--source`` with shared ``SOURCES`` choices."""
    kwargs: dict[str, object] = {"choices": list(SOURCES), "default": default}
    if help is not None:
        kwargs["help"] = help
    parser.add_argument("--source", **kwargs)


__all__ = [
    "add_db_argument",
    "add_direction_argument",
    "add_from_filter_argument",
    "add_from_to_arguments",
    "add_source_argument",
    "build_repo",
]
