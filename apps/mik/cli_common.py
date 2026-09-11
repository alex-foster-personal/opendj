"""Helpers shared by :mod:`apps.mik.cli` and its subcommand modules
(:mod:`apps.mik.cli_promote`), split out to break the import cycle a
subcommand module would otherwise have with ``cli.py`` (the subcommand
module needs these, and ``cli.py`` needs the subcommand's ``cmd_*``
function back).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.shared.equivalence import EquivalenceGate

from . import availability as avail

UNVERIFIED_FLAG = "--i-know-equivalence-is-unverified"


def _emit(payload: dict, *, as_json: bool, lines: list[str]) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for line in lines:
            print(line)


def _state_db_path(args: argparse.Namespace) -> Path:
    return avail.resolve_data_dir(args.data_dir) / "state" / "state.db"


def _gate(args: argparse.Namespace, *, mik_store: Path | None = None) -> EquivalenceGate:
    """``mik_store`` binds the gate to the MIK store THIS run is about to
    read (P1 regression, PR #383 review): without it, a verdict computed by
    ``apps.equivalence`` against one ``--mik-db`` would authorize values
    read here from a completely different store (a different ``--store``,
    or the same path with the file since upgraded), because nothing tied
    the passing status to the specific file it was proven against. Passed
    only where a store is actually read (``cmd_load``) -- ``cmd_promote``
    reads no MIK store itself, so there is nothing to bind.
    """
    return EquivalenceGate.load(
        avail.resolve_data_dir(args.data_dir),
        allow_unverified=args.i_know_equivalence_is_unverified,
        sources={"mik": mik_store} if mik_store is not None else None,
    )
