"""``opendj lighting``: clock selection, monitor/replay (no engine) and scenes/look via the daemon (LIGHT-70).

    opendj lighting clock [master|deck-N] [--json]

    opendj lighting monitor [--artnet] [--sacn] [--host H] [--artnet-port P] [--sacn-port P]
                            [--sacn-universe U ...] [--json]

    opendj lighting replay FILE [--at S] [--json]

    opendj lighting scenes [--json]
    opendj lighting look [--json]
    opendj lighting set KEY VALUE [--json]

    opendj lighting render TRACK_ID --patch NAME [--from-s S] [--to-s S] [--fps N] [--seed N]
                           [--palette P] [--response R] [--analysis-dir DIR] [--patch-file F] --out FILE

    opendj lighting patch list [--json]
    opendj lighting patch add --profile P --universe U --address A [--group G ...] [--id ID] [--mode M]
    opendj lighting patch remove FIXTURE_ID
    opendj lighting patch use NAME

``monitor`` listens for Art-Net (UDP 6454) and sACN / E1.31 (UDP 5568) and prints one line per
frame with the universe, sequence and the channels that changed since that universe's last
frame. Neither ``--artnet`` nor ``--sacn`` means both. ``--sacn-universe`` joins that universe's
multicast group; without it only unicast sACN arrives. ``--json`` prints each line as a JSON
object. Runs until interrupted.

``replay`` prints the same ``StageQuery`` read model as ``window.musicDjToolsLightingStage.query()``
for the given frames file at elapsed time ``S`` (default 0). ``--json`` prints the full query object.

``render`` plans TRACK_ID's show offline and writes a frames file (the frames websocket message
shape) without playing (LIGHT-73). It fails, writing nothing, when the track has no analysis.
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
from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE, lighting_clock_cli
from apps.opendj_cli.api_cli import REQUEST_TIMEOUT_S, exit_for_status, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning, unreachable
from apps.opendj_cli.refusal import fail as _fail

USAGE = "usage: opendj lighting clock|monitor|replay|render|scenes|look|set|patch ..."


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
    clock = verbs.add_parser("clock", help="query or select the lighting beat and phrase clock")
    clock.add_argument("source", nargs="?", help="master or deck-N (N is 1 through 4)")
    clock.add_argument("--json", action="store_true", help="print the REST clock read model")
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
    render = verbs.add_parser("render", help="render a track's planned show to a frames JSON file offline")
    render.add_argument("track_id", help="track stable id")
    render.add_argument("--patch", required=True, help="venue patch name")
    render.add_argument("--patch-file", type=Path, default=None, help="venue store JSON (default: the saved store)")
    render.add_argument("--analysis-dir", type=Path, default=Path("."), help="dir holding <track-id>.json analysis")
    render.add_argument("--from-s", type=float, default=0.0)
    render.add_argument("--to-s", type=float, default=30.0)
    render.add_argument("--fps", type=int, default=30)
    render.add_argument("--seed", type=int, default=1)
    render.add_argument(
        "--palette", default="cool", help="palette variant for frame colours (the live runtime palette)"
    )
    render.add_argument("--response", default="matched", help="energy curve variant: gentle, matched or punchy")
    render.add_argument("--out", type=Path, required=True, help="frames JSON file to write")
    verbs.add_parser("scenes", help="list scene ids from GET /api/v1/lighting/scenes")
    verbs.add_parser("look", help="read owner scene and phrase from GET /api/v1/lighting/look")
    set_verb = verbs.add_parser("set", help="set a lighting variant via PUT /api/v1/lighting/settings/KEY")
    set_verb.add_argument("key", help="setting key: palette, energy_curve, movement or handover")
    set_verb.add_argument("value", help="new value, e.g. warm, punchy or wide")
    patch = verbs.add_parser("patch", help="read or edit the venue patch via /api/v1/lighting/patch")
    patch_verbs = patch.add_subparsers(dest="patch_verb", required=True)
    patch_verbs.add_parser("list", help="list the active patch's fixtures")
    add = patch_verbs.add_parser("add", help="add a fixture to the active patch")
    add.add_argument("--profile", required=True, help="profile id, see GET /api/v1/lighting/profiles")
    add.add_argument("--universe", type=int, required=True)
    add.add_argument("--address", type=int, required=True)
    add.add_argument("--group", action="append", default=[], help="group name (repeatable)")
    add.add_argument("--id", dest="fixture_id", default=None, help="fixture id (default <profile>-<n>)")
    add.add_argument("--mode", default=None, help="profile mode (default: the profile's first mode)")
    add.add_argument("--x", type=float, default=0.5)
    add.add_argument("--y", type=float, default=0.5)
    remove = patch_verbs.add_parser("remove", help="remove a fixture from the active patch")
    remove.add_argument("fixture_id")
    use = patch_verbs.add_parser("use", help="make a named patch active (created empty when new)")
    use.add_argument("name")
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
    put_body: dict[str, Any] | None = None,
) -> tuple[int, Any | None]:
    try:
        target = resolve_backend_base_url(lock_path=lock)
    except EngineNotRunning as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE), None
    url = f"{target.base_url.rstrip('/')}{path}"
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S, trust_env=False) as client:
            headers = {"Accept": "application/json"}
            response = (
                client.get(url, headers=headers)
                if put_body is None
                else client.put(url, headers=headers, json=put_body)
            )
    except (httpx.TransportError, httpx.TimeoutException) as error:
        if target.origin is not None:
            message = str(unreachable(target.origin, error))
            return _fail(as_json, "engine_unreachable", message, EXIT_NO_ENGINE), None
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED), None
    if 200 <= response.status_code < 300:
        return EXIT_CONFIRMED, response.json()
    exit_code = exit_for_status(response.status_code)
    message = f"{'GET' if put_body is None else 'PUT'} {path} returned {response.status_code}"
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


def _run_set(key: str, value: str, *, lock: Path | None, as_json: bool) -> int:
    path = f"/api/v1/lighting/settings/{key}"
    exit_code, payload = _fetch_lighting_resource(path, lock=lock, as_json=as_json, put_body={"value": value})
    if exit_code != EXIT_CONFIRMED or not isinstance(payload, dict):
        return exit_code
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{payload.get('key')}: {payload.get('previous')} -> {payload.get('value')}")
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


def _run_render(args: argparse.Namespace) -> int:
    from apps.autodj.http import build_service
    from apps.lighting.http import (
        default_patch_path,
        dump_frames,
        load_analysis,
        load_palette,
        load_show_map,
        load_store,
        render_show,
    )

    try:
        profile, anlz = load_analysis(args.analysis_dir, args.track_id, autodj=build_service())
        store = load_store(args.patch_file or default_patch_path()).with_active(args.patch)
        rendered = render_show(
            profile,
            anlz,
            store,
            load_show_map(),
            load_palette(args.palette),
            from_s=args.from_s,
            to_s=args.to_s,
            fps=args.fps,
            seed=args.seed,
            response=args.response,
        )
    except (KeyError, OSError, ValueError, TypeError) as error:
        print(f"opendj lighting render: {error}", file=sys.stderr)
        return EXIT_FAILED
    args.out.write_text(dump_frames(rendered), encoding="utf-8")
    print(f"wrote {len(rendered['frames'])} frames to {args.out}")
    return EXIT_CONFIRMED


def _active_fixtures(store: dict[str, Any]) -> list[dict[str, Any]]:
    for patch in store.get("patches", []):
        if patch.get("name") == store.get("active"):
            fixtures: list[dict[str, Any]] = patch["fixtures"]
            return fixtures
    raise TypeError(f"active patch {store.get('active')!r} missing from response")


def _edit_store(args: argparse.Namespace, store: dict[str, Any], profiles: list[dict[str, Any]]) -> str | None:
    """Apply one patch edit to ``store`` in place; returns an error message or None."""
    if args.patch_verb == "use":
        if args.name not in {p.get("name") for p in store["patches"]}:
            store["patches"].append({"name": args.name, "fixtures": []})
        store["active"] = args.name
        return None
    fixtures = _active_fixtures(store)
    if args.patch_verb == "remove":
        kept = [f for f in fixtures if f.get("id") != args.fixture_id]
        if len(kept) == len(fixtures):
            return f"no fixture {args.fixture_id!r} in patch {store['active']!r}"
        fixtures[:] = kept
        return None
    profile = next((p for p in profiles if p.get("id") == args.profile), None)
    if profile is None:
        return f"unknown profile {args.profile!r}; profiles: {sorted(p.get('id') for p in profiles)}"
    taken = {f.get("id") for f in fixtures}
    fixture_id = args.fixture_id or next(
        f"{args.profile}-{n}" for n in range(1, len(taken) + 2) if f"{args.profile}-{n}" not in taken
    )
    fixtures.append(
        {
            "id": fixture_id,
            "profile": args.profile,
            "mode": args.mode or next(iter(profile["modes"])),
            "universe": args.universe,
            "address": args.address,
            "groups": list(args.group),
            "position": {"x": args.x, "y": args.y},
        }
    )
    return None


def _run_patch(args: argparse.Namespace, *, lock: Path | None, as_json: bool) -> int:
    path = "/api/v1/lighting/patch"
    exit_code, store = _fetch_lighting_resource(path, lock=lock, as_json=as_json)
    if exit_code != EXIT_CONFIRMED or not isinstance(store, dict):
        return exit_code
    if args.patch_verb == "list":
        if as_json:
            print(json.dumps(store, indent=2, sort_keys=True))
            return EXIT_CONFIRMED
        print(f"active: {store.get('active')}")
        for f in _active_fixtures(store):
            print(f"{f['id']}\t{f['profile']}\t{f['mode']}\tu{f['universe']}@{f['address']}\t{','.join(f['groups'])}")
        return EXIT_CONFIRMED
    profiles: list[dict[str, Any]] = []
    if args.patch_verb == "add":
        exit_code, payload = _fetch_lighting_resource("/api/v1/lighting/profiles", lock=lock, as_json=as_json)
        if exit_code != EXIT_CONFIRMED or not isinstance(payload, list):
            return exit_code
        profiles = payload
    error = _edit_store(args, store, profiles)
    if error is not None:
        return _fail(as_json, "invalid_request", error, EXIT_FAILED)
    exit_code, saved = _fetch_lighting_resource(path, lock=lock, as_json=as_json, put_body=store)
    if exit_code != EXIT_CONFIRMED:
        return exit_code
    if as_json:
        print(json.dumps(saved, indent=2, sort_keys=True))
    else:
        print(f"saved patch {store['active']!r} ({len(_active_fixtures(store))} fixtures)")
    return EXIT_CONFIRMED


def run(argv: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    """Entry point from ``opendj lighting ...``; returns the process exit code."""
    args = _parser().parse_args(list(argv))
    if args.verb == "clock":
        return lighting_clock_cli.run(args.source, as_json=as_json or args.json, lock=lock)
    if args.verb == "scenes":
        return _run_http_get("/api/v1/lighting/scenes", _print_scenes, lock=lock, as_json=as_json)
    if args.verb == "look":
        return _run_http_get("/api/v1/lighting/look", _print_look, lock=lock, as_json=as_json)
    if args.verb == "set":
        return _run_set(args.key, args.value, lock=lock, as_json=as_json)
    if args.verb == "monitor":
        return _run_monitor(args, as_json)
    if args.verb == "replay":
        return _run_replay(args, as_json)
    if args.verb == "render":
        return _run_render(args)
    if args.verb == "patch":
        return _run_patch(args, lock=lock, as_json=as_json)
    raise AssertionError(f"unhandled verb {args.verb!r}")
