"""`python -m apps.analysis.selection {show,set-default,set-toggle}`.

A THIN CLIENT over ``/api/v1/analysis/source``, never a second writer.

The dev toggle is in-memory and process-local (spec section 3), so a CLI
that mutated its own import of :mod:`apps.analysis.selection` would set the
state in a python process that exits a millisecond later, leave the running
server on the old state, and print success. That is a lie the agent-native
parity this lane promises cannot afford, so every verb here goes over HTTP
to the one process that holds the state.

When the service is unreachable the CLI FAILS LOUDLY and names the URL it
tried, rather than falling back to a local mutation.

Exit codes: 0 ok, 2 usage, 3 service unreachable or refused.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .lanes import LANES
from .selection import SOURCES, TOGGLE_STATES

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3

_TIMEOUT_S = 10.0


class PortResolutionError(RuntimeError):
    """This worktree's backend port could not be determined."""


def base_url() -> str:
    """This worktree's backend base URL, via `just webui-ports`.

    Deliberately NOT `from apps.webui.port_config import resolve_ports`.
    `apps.analysis` is a domain package and `.importlinter`'s
    `webui-is-the-top-layer` contract forbids a domain package importing the
    delivery layer -- it is one of the few checks in this repo that fails
    hard at zero rather than ratcheting, and its own comment says "Never add
    a line here: add the inversion instead". A CLI shelling out to the
    documented command is the inversion: same source of truth, no edge.

    `MUSIC_DJ_BACKEND_PORT` short-circuits it, which is what the daemon and
    every recipe already export, so the subprocess is the fallback rather
    than the common path.
    """
    from_env = os.environ.get("MUSIC_DJ_BACKEND_PORT", "").strip()
    if from_env:
        return f"http://127.0.0.1:{_port(from_env)}/api/v1"

    just = shutil.which("just")
    if just is None:
        raise PortResolutionError(
            "MUSIC_DJ_BACKEND_PORT is unset and `just` is not on PATH, so this "
            "worktree's backend port cannot be resolved. Export the port, or "
            "run `just webui-ports-claim` from the worktree first."
        )
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [just, "--justfile", str(root / "justfile"), "webui-ports"],
        capture_output=True, text=True, cwd=root, timeout=60, check=False,
    )
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "backend":
            return parts[1].rstrip("/") + "/api/v1"
    raise PortResolutionError(
        f"`just webui-ports` printed no `backend <url>` line (exit {proc.returncode}). "
        f"stdout: {proc.stdout.strip()[:400]!r} stderr: {proc.stderr.strip()[:400]!r}"
    )


def _port(raw: str) -> int:
    try:
        port = int(raw)
    except ValueError as exc:
        raise PortResolutionError(
            f"MUSIC_DJ_BACKEND_PORT is {raw!r}, which is not a port number"
        ) from exc
    if not 1 <= port <= 65535:
        raise PortResolutionError(f"MUSIC_DJ_BACKEND_PORT {port} is out of range")
    return port


def _request(url: str, *, method: str, body: dict | None = None) -> dict:
    data = None
    headers = {"accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def _print_state(state: dict) -> None:
    print(f"{'lane':<10} {'default':<8} {'toggle':<8} effective")
    for lane, block in state["lanes"].items():
        print(
            f"{lane:<10} {block['default']:<8} {block['toggle']:<8} "
            f"{block['effective']}"
        )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apps.analysis.selection",
        description="Read and set the per-lane analysis source over the running "
                    "backend's HTTP API. Never mutates local state.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print every lane's default, toggle and effective source")
    p_default = sub.add_parser(
        "set-default", help="persist a lane's default source (survives relaunch)"
    )
    p_default.add_argument("lane", choices=list(LANES))
    p_default.add_argument("source", choices=list(SOURCES))
    p_toggle = sub.add_parser(
        "set-toggle", help="set a lane's in-memory dev toggle (resets to unset on relaunch)"
    )
    p_toggle.add_argument("lane", choices=list(LANES))
    p_toggle.add_argument("state", choices=list(TOGGLE_STATES))

    args = parser.parse_args(argv)

    try:
        url = f"{base_url()}/analysis/source"
    except (PortResolutionError, subprocess.SubprocessError, OSError) as exc:
        print(f"[ERROR] cannot resolve this worktree's backend port: {exc}")
        return EXIT_UNREACHABLE

    try:
        if args.command == "show":
            state = _request(url, method="GET")
        elif args.command == "set-default":
            state = _request(
                url, method="PUT", body={"lane": args.lane, "default": args.source}
            )
        elif args.command == "set-toggle":
            state = _request(
                url, method="PUT", body={"lane": args.lane, "toggle": args.state}
            )
        else:
            parser.error(f"unhandled command {args.command!r}")
            return EXIT_USAGE
    except urllib.error.HTTPError as exc:
        print(
            f"[ERROR] {args.command} refused by {url}: "
            f"HTTP {exc.code} {exc.read().decode()[:400]}"
        )
        return EXIT_UNREACHABLE
    except (urllib.error.URLError, OSError) as exc:
        print(
            f"[ERROR] backend unreachable at {url}: {exc}. Start it with "
            "`just webui-backend` in this worktree; the dev toggle lives in that "
            "process, so there is nothing this CLI can set without it."
        )
        return EXIT_UNREACHABLE

    _print_state(state)
    return EXIT_OK


__all__ = [
    "EXIT_OK",
    "EXIT_UNREACHABLE",
    "EXIT_USAGE",
    "PortResolutionError",
    "base_url",
    "main",
]
