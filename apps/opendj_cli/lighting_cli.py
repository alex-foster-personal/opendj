"""``opendj lighting``: lighting tools that run without the engine (LIGHT-70, LIGHT-10573).

    opendj lighting monitor [--artnet] [--sacn] [--host H] [--artnet-port P] [--sacn-port P]
                            [--sacn-universe U ...] [--json]

    opendj lighting replay FILE [--at S] [--json]

``monitor`` listens for Art-Net (UDP 6454) and sACN / E1.31 (UDP 5568) and prints one line per
frame with the universe, sequence and the channels that changed since that universe's last
frame. Neither ``--artnet`` nor ``--sacn`` means both. ``--sacn-universe`` joins that universe's
multicast group; without it only unicast sACN arrives. ``--json`` prints each line as a JSON
object. Runs until interrupted.

``replay`` prints the same ``StageQuery`` read model as ``window.musicDjToolsLightingStage.query()``
for the given frames file at elapsed time ``S`` (default 0). ``--json`` prints the full query object.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from apps.lighting.http import ARTNET_PORT, SACN_PORT, MonitorConfig, replay_query, start_monitor
from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED

USAGE = "usage: opendj lighting monitor|replay ..."


def _finite_at(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--at must be a finite float") from error
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("--at must be a finite float")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opendj lighting", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    verbs = parser.add_subparsers(dest="verb", required=True)
    monitor = verbs.add_parser("monitor", help="print Art-Net and sACN frames as per-universe changes")
    monitor.add_argument("--artnet", action="store_true", help="listen for Art-Net")
    monitor.add_argument("--sacn", action="store_true", help="listen for sACN / E1.31")
    monitor.add_argument("--host", default="0.0.0.0", help="address to bind (default 0.0.0.0)")
    monitor.add_argument("--artnet-port", type=int, default=ARTNET_PORT)
    monitor.add_argument("--sacn-port", type=int, default=SACN_PORT)
    monitor.add_argument(
        "--sacn-universe", type=int, action="append", default=[], help="join this universe's multicast group"
    )
    monitor.add_argument("--json", action="store_true", help="print each frame as a JSON object")
    replay = verbs.add_parser(
        "replay",
        help="print stage replay query for a frames JSON file (same read model as the /lighting IPC)",
    )
    replay.add_argument("file", help="frames JSON replay file")
    replay.add_argument(
        "--at",
        type=_finite_at,
        default=0.0,
        help="elapsed seconds into the replay (default 0)",
    )
    replay.add_argument("--json", action="store_true", help="print the full StageQuery JSON")
    return parser


def config_from_args(args: argparse.Namespace, as_json: bool) -> MonitorConfig:
    """The listener config for parsed ``monitor`` args; neither protocol flag means both."""
    both = not args.artnet and not args.sacn
    return MonitorConfig(
        host=args.host,
        artnet_port=args.artnet_port if (args.artnet or both) else None,
        sacn_port=args.sacn_port if (args.sacn or both) else None,
        sacn_universes=tuple(args.sacn_universe),
        as_json=as_json or args.json,
    )


def _print_line(line: str) -> None:
    print(line, flush=True)


async def _monitor_forever(config: MonitorConfig) -> None:
    monitor = await start_monitor(config, _print_line)
    listening = ", ".join(f"{name} {config.host}:{port}" for name, port in monitor.ports.items())
    print(f"listening on {listening}", file=sys.stderr, flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        monitor.close()


def _replay_text_summary(query: dict[str, object]) -> str:
    name = query.get("replay_name") or "replay"
    index = query.get("frame_index")
    count = query.get("frame_count")
    elapsed = query.get("elapsed_s")
    duration = query.get("duration_s")
    lines = [f"replay {name}: frame {index}/{count} at {elapsed} s of {duration} s"]
    frame = query.get("frame")
    if isinstance(frame, dict):
        fixtures = frame.get("fixtures")
        if isinstance(fixtures, list):
            for fx in fixtures:
                if not isinstance(fx, dict):
                    continue
                fid = fx.get("id", "?")
                rgb = fx.get("rgb", [0, 0, 0])
                if not isinstance(rgb, list) or len(rgb) < 3:
                    rgb = [0, 0, 0]
                lines.append(
                    f"{fid} rgb({rgb[0]}, {rgb[1]}, {rgb[2]}) intensity {fx.get('intensity')} "
                    f"pan {fx.get('pan_deg')} tilt {fx.get('tilt_deg')} strobe {fx.get('strobe_hz')}"
                )
    return "\n".join(lines)


def run(argv: Sequence[str], *, as_json: bool = False) -> int:
    """Entry point from ``opendj lighting ...``; returns the process exit code."""
    args = _parser().parse_args(list(argv))
    if args.verb == "monitor":
        try:
            asyncio.run(_monitor_forever(config_from_args(args, as_json)))
        except KeyboardInterrupt:
            return EXIT_CONFIRMED
        except OSError as error:
            print(f"opendj lighting monitor: cannot listen: {error}", file=sys.stderr)
            return EXIT_FAILED
        return EXIT_CONFIRMED
    if args.verb == "replay":
        path = Path(args.file)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            print(f"opendj lighting replay: cannot read {args.file}: {error}", file=sys.stderr)
            return EXIT_FAILED
        try:
            query = replay_query(text, at_s=args.at, name=path.name)
        except (TypeError, ValueError) as error:
            print(f"opendj lighting replay: {error}", file=sys.stderr)
            return EXIT_FAILED
        if as_json or args.json:
            print(json.dumps(query))
        else:
            print(_replay_text_summary(query))
        return EXIT_CONFIRMED
    raise AssertionError(f"unhandled verb {args.verb!r}")
