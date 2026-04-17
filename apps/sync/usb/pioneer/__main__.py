"""CLI: ``python -m apps.sync.usb.pioneer <subcommand>``.

Subcommands
-----------

``read <path>``
    Dump the full :func:`read_usb_export` output as pretty JSON to stdout.

    ``--validate`` / ``-v``
        Also run :func:`validate_invariants` and include a
        ``validation`` key in the output with ``errors`` / ``ok`` fields.
        Exit code is ``0`` on success, ``2`` if any invariant fails.

``agent-export --playlist NAME --usb PATH [--dry-run | --live]``
    Prototype C: run the Claude Sonnet computer-use agent to drive
    Rekordbox 7's export flow. ``--dry-run`` (default) walks through
    the flow but skips the final Export click. ``--live`` actually
    triggers the USB write. Traces are written to
    ``apps/sync/usb/pioneer/traces/<timestamp>/``.

Requirement: CAT-06.
"""
from __future__ import annotations

import argparse
import json
import logging
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


def _cmd_agent_export(args: argparse.Namespace) -> int:
    # Lazy import: the agent module pulls in anthropic + Quartz which
    # are not needed for the read subcommand.
    from .agent import AgentConfig, run_export_agent

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg = AgentConfig(
        playlist=args.playlist,
        usb_path=args.usb,
        dry_run=not args.live,
        max_steps=args.max_steps,
    )

    print(f"→ Playlist:  {cfg.playlist!r}", file=sys.stderr)
    print(f"→ USB:       {cfg.usb_path!r}", file=sys.stderr)
    print(f"→ Mode:      {'LIVE' if args.live else 'dry-run'}", file=sys.stderr)
    print(f"→ Max steps: {cfg.max_steps}", file=sys.stderr)

    result = run_export_agent(cfg)

    summary = {
        "success": result.success,
        "model": result.model,
        "steps": result.steps,
        "stop_reason": result.stop_reason,
        "trace_root": str(result.trace_root),
        "usage_in_tokens": result.usage_in_tokens,
        "usage_out_tokens": result.usage_out_tokens,
        "estimated_cost_usd": round(result.estimated_cost_usd, 4),
        "final_text": result.final_text,
        "actions": result.action_summaries,
    }
    json.dump(summary, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0 if result.success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.sync.usb.pioneer",
        description="Pioneer USB export reader + export agent (CAT-06).",
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

    agent = sub.add_parser(
        "agent-export",
        help="Drive Rekordbox 7 export via Claude computer-use agent.",
    )
    agent.add_argument(
        "--playlist",
        required=True,
        help="Name of the Rekordbox playlist to export.",
    )
    agent.add_argument(
        "--usb",
        required=True,
        help="Path of the mounted USB volume (e.g. /Volumes/MAINTAINER).",
    )
    mode = agent.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="live",
        action="store_false",
        default=False,
        help="(default) Walk through the flow but skip the final export click.",
    )
    mode.add_argument(
        "--live",
        dest="live",
        action="store_true",
        help="Allow the agent to actually trigger the export.",
    )
    agent.add_argument(
        "--max-steps",
        type=int,
        default=30,
        help="Hard cap on agent iterations (default 30).",
    )
    agent.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable INFO-level logging.",
    )
    agent.set_defaults(func=_cmd_agent_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
