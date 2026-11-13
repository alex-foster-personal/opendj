"""``opendj library protection [--no-delete on|off] [--no-edit on|off]`` (LIBM-57).

Agent twin of ``GET/PUT /api/v1/library/protection``. With no flag it prints the two
locks; with flags it sets the named ones and prints the result. Reads and writes the
same ``library-protection.json`` beside the state DB that the engine's guard re-reads on
every playlist write, so no running engine is needed and a change applies at once.

Requirements:
- ✔︎ ✅ 🎯 [if] ``--no-delete on`` is given [then ⛔️] the stored setting still has ``no_delete`` off
- ✔︎ ✅ 🎯 [if] no flag is given [then ⛔️] the stored setting changes
- ✔︎ ✅ 🎯 [if] ``--no-edit maybe`` is given [then ⛔️] exit 0 or a file is written
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED
from apps.opendj_cli.refusal import fail as _fail
from apps.opendj_cli.track_cli import _resolve_state_db
from apps.webui.server.library_protection import (
    LibraryProtection,
    LibraryProtectionPatch,
    ProtectionFileInvalid,
    load_protection,
    protection_path,
    update_protection,
)

USAGE = "usage: opendj library protection [--no-delete on|off] [--no-edit on|off]"
_SWITCH = {"on": True, "off": False}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise ValueError(message)


def _parser() -> _Parser:
    parser = _Parser(prog="opendj library protection", add_help=False)
    parser.add_argument("--no-delete", choices=tuple(_SWITCH))
    parser.add_argument("--no-edit", choices=tuple(_SWITCH))
    return parser


def _print(protection: LibraryProtection, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(protection.model_dump(), indent=2, sort_keys=True))
        return
    print(f"no_delete {'on' if protection.no_delete else 'off'}, no_edit {'on' if protection.no_edit else 'off'}")


def run(tokens: Sequence[str], *, as_json: bool, state_db: Path | None) -> int:
    """Print the Library Protection locks, or set the named ones first."""
    try:
        args = _parser().parse_args(list(tokens))
    except ValueError as exc:
        return _fail(as_json, "usage", f"{exc}; {USAGE}", EXIT_FAILED)
    path = protection_path(_resolve_state_db(state_db))
    patch = LibraryProtectionPatch(
        no_delete=None if args.no_delete is None else _SWITCH[args.no_delete],
        no_edit=None if args.no_edit is None else _SWITCH[args.no_edit],
    )
    read_only = patch.no_delete is None and patch.no_edit is None
    try:
        protection = load_protection(path) if read_only else update_protection(path, patch)
    except ProtectionFileInvalid as exc:
        return _fail(as_json, "library_protection_invalid", str(exc), EXIT_FAILED)
    _print(protection, as_json=as_json)
    return EXIT_CONFIRMED
