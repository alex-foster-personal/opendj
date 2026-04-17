"""CLI: ``python -m apps.sync.usb.pioneer read <pioneer-path>``.

Subcommands
-----------

``read <path>``
    Dump the full :func:`read_usb_export` output as pretty JSON to stdout.

    ``--validate`` / ``-v``
        Also run :func:`validate_invariants` and include a
        ``validation`` key in the output with ``errors`` / ``ok`` fields.
        Exit code is ``0`` on success, ``2`` if any invariant fails.

Requirement: CAT-06.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .reader import read_usb_export, validate_invariants


def _cmd_read(args: argparse.Namespace) -> int:
    data = read_usb_export(Path(args.path))
    if args.validate:
        errors = validate_invariants(data)
        data["validation"] = {"ok": not errors, "errors": errors}
    json.dump(data, sys.stdout, indent=2, sort_keys=False, default=str)
    sys.stdout.write("\n")
    if args.validate and data["validation"]["errors"]:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.sync.usb.pioneer",
        description="Pioneer USB export reader (CAT-06 Prototype A).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    read = sub.add_parser("read", help="Dump a USB export as JSON.")
    read.add_argument("path", help="Path to /PIONEER or its parent.")
    read.add_argument(
        "--validate",
        "-v",
        action="store_true",
        help="Run invariant checks and include a 'validation' key in the output.",
    )
    read.set_defaults(func=_cmd_read)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
