"""``opendj lighting``: monitor/replay (no engine) and scenes/look via the daemon (LIGHT-70).

    opendj lighting monitor [--artnet] [--sacn] [--host H] [--artnet-port P] [--sacn-port P]
                            [--sacn-universe U ...] [--json]

    opendj lighting replay FILE [--at S] [--json]

    opendj lighting scenes [--json]
    opendj lighting look [--json]

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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

from apps.lighting.http import ARTNET_PORT, SACN_PORT, MonitorConfig, replay_query, start_monitor
from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import REQUEST_TIMEOUT_S, exit_for_status, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning, unreachable
from apps.opendj_cli.refusal import fail as _fail

USAGE = "usage: opendj lighting monitor|replay|scenes|look ..."


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
    verbs.add_parser("scenes", help="list scene ids from GET /api/v1/lighting/scenes")
    verbs.add_parser("look", help="read owner scene and phrase from GET /api/v1/lighting/look")
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


def _fetch_lighting_resource(
    path: str,
    *,
    lock: Path | None,
    as_json: bool,
) -> tuple[int, Any | None]:
    try:
        target = resolve_backend_base_url(lock_path=lock)
    except EngineNotRunning as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE), None
    url = f"{target.base_url.rstrip('/')}{path}"
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S, trust_env=False) as client:
            response = client.get(url, headers={"Accept": "application/json"})
    except (httpx.TransportError, httpx.TimeoutException) as error:
        if target.origin is not None:
            message = str(unreachable(target.origin, error))
            return _fail(as_json, "engine_unreachable", message, EXIT_NO_ENGINE), None
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED), None
    if 200 <= response.status_code < 300:
        return EXIT_CONFIRMED, response.json()
    exit_code = exit_for_status(response.status_code)
    message = f"GET {path} returned {response.status_code}"
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("detail", body)
            if isinstance(detail, dict):
                flag = detail.get("flag", "")
                text = detail.get("message", "")
                if isinstance(text, str) and text:
                    message = text
                elif isinstance(flag, str) and flag:
                    message = f"feature flag {flag} is off"
    except json.JSONDecodeError:
        if response.text:
            message = response.text.strip()
    return _fail(as_json, "http_error", message, exit_code), None


def _print_scenes(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not isinstance(payload, list):
        raise TypeError("scenes response must be a JSON array")
    for row in payload:
        if isinstance(row, dict):
            print(f"{row.get('id')}\t{row.get('name')}")


def _print_look(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not isinstance(payload, dict):
        raise TypeError("look response must be a JSON object")
    print(f"scene_id: {payload.get('scene_id')}")
    print(f"phrase_label: {payload.get('phrase_label')}")


def _run_http_get(
    path: str,
    print_fn: Callable[..., None],
    *,
    lock: Path | None,
    as_json: bool,
) -> int:
    exit_code, payload = _fetch_lighting_resource(path, lock=lock, as_json=as_json)
    if exit_code != EXIT_CONFIRMED or payload is None:
        return exit_code
    try:
        print_fn(payload, as_json=as_json)
    except TypeError as error:
        return _fail(as_json, "invalid_response", str(error), EXIT_FAILED)
    return EXIT_CONFIRMED


def _run_monitor(args: argparse.Namespace, as_json: bool) -> int:
    try:
        asyncio.run(_monitor_forever(config_from_args(args, as_json)))
    except KeyboardInterrupt:
        return EXIT_CONFIRMED
    except OSError as error:
        print(f"opendj lighting monitor: cannot listen: {error}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_CONFIRMED


def _run_replay(args: argparse.Namespace, as_json: bool) -> int:
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


def run(argv: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    """Entry point from ``opendj lighting ...``; returns the process exit code."""
    args = _parser().parse_args(list(argv))
    if args.verb == "scenes":
        return _run_http_get("/api/v1/lighting/scenes", _print_scenes, lock=lock, as_json=as_json)
    if args.verb == "look":
        return _run_http_get("/api/v1/lighting/look", _print_look, lock=lock, as_json=as_json)
    if args.verb == "monitor":
        return _run_monitor(args, as_json)
    if args.verb == "replay":
        return _run_replay(args, as_json)
    raise AssertionError(f"unhandled verb {args.verb!r}")
