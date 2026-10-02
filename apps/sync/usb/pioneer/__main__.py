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

``write --template PATH --output PATH [--playlist NAME:ID,...]
        [--track ID:FIELD=VALUE,...] [--apply]``
    Prototype B: copy an existing Rekordbox-produced OneLibrary
    (``exportLibrary.db``) to ``--output`` and overlay zero or more
    new playlists + track metadata updates through our own SQLCipher
    handle. Defaults to dry-run (plan only). Pass ``--apply`` to
    invoke the writer. See :mod:`apps.sync.usb.pioneer.writer_onelibrary`
    for the capability matrix + safety guards.

Requirement: CAT-06.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from apps.shared.rekordbox_writeback import require_writeback_enabled

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


# -----------------------------------------------------------------------
# ``write`` subcommand (Prototype B: OneLibrary overlay writer).
# -----------------------------------------------------------------------

_WRITE_TRACK_FIELDS = {"title", "rating", "bpmx100", "dj_comment", "color_id"}


def _parse_playlist_spec(raw: str) -> tuple[str, list[int]]:
    """Parse a ``NAME:ID,ID,ID`` spec into ``(name, [int, ...])``.

    Raises :class:`ValueError` with a human-readable message on bad
    input. The name is allowed to contain ``:`` characters as long as
    the rightmost ``:`` splits name from the id list.
    """
    if ":" not in raw:
        raise ValueError(
            f"playlist spec must contain ':' separating NAME from IDs: {raw!r}"
        )
    name, _, ids_part = raw.rpartition(":")
    name = name.strip()
    if not name:
        raise ValueError(f"playlist name is empty in spec: {raw!r}")
    ids_part = ids_part.strip()
    if not ids_part:
        return name, []
    try:
        ids = [int(x.strip()) for x in ids_part.split(",") if x.strip()]
    except ValueError as exc:
        raise ValueError(
            f"playlist track ids must be integers: {raw!r} ({exc})"
        ) from exc
    return name, ids


def _parse_track_spec(raw: str) -> tuple[int, dict[str, Any]]:
    """Parse a ``ID:FIELD=VALUE,FIELD=VALUE`` spec.

    ``rating``, ``bpmx100``, and ``color_id`` are coerced to ``int``;
    every other whitelisted field stays as ``str``. Unknown fields
    raise :class:`ValueError`.
    """
    if ":" not in raw:
        raise ValueError(
            f"track spec must contain ':' separating ID from FIELD=VALUE list: {raw!r}"
        )
    id_part, _, rest = raw.partition(":")
    try:
        track_id = int(id_part.strip())
    except ValueError as exc:
        raise ValueError(
            f"track id must be an integer in spec: {raw!r} ({exc})"
        ) from exc
    overlay: dict[str, Any] = {}
    for chunk in rest.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(
                f"track field must be FIELD=VALUE: {chunk!r} (in {raw!r})"
            )
        field, _, value = chunk.partition("=")
        field = field.strip()
        value = value.strip()
        if field not in _WRITE_TRACK_FIELDS:
            raise ValueError(
                f"unknown track field {field!r}; "
                f"expected one of {sorted(_WRITE_TRACK_FIELDS)}"
            )
        if field in {"rating", "bpmx100", "color_id"}:
            try:
                overlay[field] = int(value)
            except ValueError as exc:
                raise ValueError(
                    f"track field {field!r} must be an integer: {value!r} ({exc})"
                ) from exc
        else:
            overlay[field] = value
    return track_id, overlay


def _cmd_write(args: argparse.Namespace) -> int:
    # Lazy import: writer_onelibrary pulls in sqlcipher3, a compiled wheel
    # that is not always installed on every dev host. Parsing + --help must
    # still work without it.
    try:
        playlist_specs = [_parse_playlist_spec(p) for p in (args.playlist or [])]
        track_specs = [_parse_track_spec(t) for t in (args.track or [])]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    template = Path(args.template).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()

    plan = {
        "mode": "apply" if args.apply else "dry-run",
        "template": str(template),
        "output": str(output),
        "playlists": [
            {"name": name, "track_ids": ids} for name, ids in playlist_specs
        ],
        "track_updates": [
            {"id": tid, "overlay": overlay} for tid, overlay in track_specs
        ],
    }

    if not args.apply:
        json.dump(plan, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    # Apply path: import the writer lazily.
    require_writeback_enabled("module.sync.usb.pioneer.cli_write")
    try:
        from .writer_onelibrary import (
            OneLibraryWriteError,
            PlaylistSpec,
            TrackUpdate,
            write_onelibrary,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"error: writer_onelibrary import failed ({exc}); "
            "install the repository dependencies (sqlcipher3 via pyrekordbox).",
            file=sys.stderr,
        )
        return 4

    playlists = [
        PlaylistSpec(name=name, track_ids=tuple(ids)) for name, ids in playlist_specs
    ]
    track_updates = []
    for tid, overlay in track_specs:
        kwargs: dict[str, Any] = {"id": tid}
        for field, value in overlay.items():
            kwargs[field] = value
        track_updates.append(TrackUpdate(**kwargs))

    try:
        result = write_onelibrary(
            template_path=template,
            output_path=output,
            track_updates=track_updates,
            playlists=playlists,
            overwrite=args.overwrite,
        )
    except OneLibraryWriteError as exc:
        print(f"error: OneLibrary write failed: {exc}", file=sys.stderr)
        return 4

    summary = {
        "mode": "apply",
        "output_path": str(result.output_path),
        "tracks_updated": result.tracks_updated,
        "playlists_written": result.playlists_written,
        "playlist_ids": list(result.playlist_ids),
        "output_size_bytes": result.output_size_bytes,
        "backend": result.backend,
    }
    json.dump(summary, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


def _cmd_diff_matrix(args: argparse.Namespace) -> int:
    """Run the structural diff matrix across all discoverable fixtures.

    Writes a markdown matrix (to ``--markdown`` or stdout) and an
    optional JSON file (``--json``). Exit code is ``0`` when every
    available fixture's rows are OK or skipped, and ``2`` when any
    fixture shows an unexpected divergence (so this can drop into CI
    directly). ``error`` rows also map to exit code ``2`` because they
    indicate a fixture the user expected to diff but couldn't.
    """
    from .differ import (
        render_matrix_json,
        render_matrix_markdown,
        run_matrix,
    )

    rows = run_matrix(
        fixture_glob=args.fixture_glob,
        include_overlay=not args.no_overlay,
    )
    md = render_matrix_markdown(rows)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(md, encoding="utf-8")
        print(f"[i] Wrote {args.markdown}", file=sys.stderr)
    else:
        sys.stdout.write(md)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(render_matrix_json(rows), encoding="utf-8")
        print(f"[i] Wrote {args.json}", file=sys.stderr)

    bad = [
        r for r in rows
        if r.status not in ("ok", "skipped")
        or r.verdict == "unexpected_divergence"
    ]
    return 2 if bad else 0


def _cmd_agent_export(args: argparse.Namespace) -> int:
    # Gated whole, not just --live: this subcommand drives the REAL rekordbox
    # GUI with synthetic clicks, and a mis-click in a dry run can still mutate
    # the library. One-way import mode refuses to operate rekordbox at all.
    require_writeback_enabled("module.sync.usb.pioneer.agent_export")
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

    # Opt-in quit-after (MDT_RB_QUIT_AFTER=1): Rekordbox idles at
    # multi-GB RSS, so close it once the export is confirmed done.
    # Default OFF (no hidden defaults); strict parse (fail fast).
    quit_after_raw = os.environ.get("MDT_RB_QUIT_AFTER", "0")
    if quit_after_raw not in {"0", "1"}:
        raise SystemExit(
            f"MDT_RB_QUIT_AFTER must be '0' or '1', got {quit_after_raw!r}"
        )
    quit_after = quit_after_raw == "1"

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
    if quit_after:
        if result.success:
            from .agent_actuator import quit_app

            quit_ok = quit_app("rekordbox")
            summary["quit_after"] = quit_ok
            print(
                "→ Quit-after: rekordbox closed"
                if quit_ok
                else "→ Quit-after: [WARN] rekordbox still running after "
                "20s (export dialog may be up) - close it manually",
                file=sys.stderr,
            )
        else:
            summary["quit_after"] = False
            print(
                "→ Quit-after skipped: export did not succeed, leaving "
                "rekordbox open for inspection",
                file=sys.stderr,
            )
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

    write = sub.add_parser(
        "write",
        help=(
            "Overlay playlists + track updates on a OneLibrary template "
            "(Prototype B; requires sqlcipher3)."
        ),
        description=(
            "Copy a Rekordbox-produced exportLibrary.db template to "
            "--output and overlay zero or more new playlists + track "
            "metadata updates via our SQLCipher handle. Dry-run by "
            "default; pass --apply to invoke the writer."
        ),
    )
    write.add_argument(
        "--template",
        required=True,
        help="Path to an existing exportLibrary.db OneLibrary template.",
    )
    write.add_argument(
        "--output",
        required=True,
        help="Destination path for the new OneLibrary (must differ from --template).",
    )
    write.add_argument(
        "--playlist",
        action="append",
        default=[],
        metavar="NAME:ID,ID,...",
        help=(
            "Playlist to create in the output DB. Format 'NAME:ID,ID,...' "
            "where IDs are content row primary keys from the template. "
            "Repeatable."
        ),
    )
    write.add_argument(
        "--track",
        action="append",
        default=[],
        metavar="ID:FIELD=VALUE,...",
        help=(
            "Track metadata overlay for an existing content row. "
            "Format 'ID:FIELD=VALUE,FIELD=VALUE'. Known fields: "
            "title, rating, bpmx100, dj_comment, color_id. Repeatable."
        ),
    )
    write.add_argument(
        "--apply",
        action="store_true",
        help="Actually invoke the writer. Default is a dry-run plan to stdout.",
    )
    write.add_argument(
        "--no-overwrite",
        dest="overwrite",
        action="store_false",
        default=True,
        help="Refuse to overwrite --output if it already exists (default: overwrite).",
    )
    write.set_defaults(func=_cmd_write)

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

    diff = sub.add_parser(
        "diff-matrix",
        help=(
            "Run the structural diff matrix across every discoverable "
            "rb-usb-export* fixture (identity + overlay round-trip)."
        ),
        description=(
            "For each fixture under tests/fixtures/ that matches the "
            "--fixture-glob pattern (directory OR .extern marker), "
            "pass the fixture's exportLibrary.db through the OneLibrary "
            "writer (identity passthrough + overlay with one test "
            "playlist) and diff the snapshots. Emits a markdown matrix "
            "and optionally a JSON side-channel. Fixtures whose "
            "external host is unmounted are marked 'skipped' and do "
            "not fail the run."
        ),
    )
    diff.add_argument(
        "--fixture-glob",
        default="rb-usb-export*",
        help="Glob against fixture names (default: %(default)s).",
    )
    diff.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="Write the markdown matrix here (default: stdout).",
    )
    diff.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Also emit a JSON side-channel matrix at this path.",
    )
    diff.add_argument(
        "--no-overlay",
        action="store_true",
        help="Skip the overlay round-trip; run identity only.",
    )
    diff.set_defaults(func=_cmd_diff_matrix)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
