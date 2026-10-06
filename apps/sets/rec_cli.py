"""``python -m apps.sets rec``: drive the RUNNING app's REC over HTTP (SET-12).

The in-process ``start`` command runs its own recorder and can never record
the master mix, which only the app's page can tap. ``rec`` is the REC
picker's agent twin: it calls the same routes the picker calls, with the same
explicit ``--source``, so a script and the button cannot disagree.

    python -m apps.sets rec start --base-url http://127.0.0.1:8702 --source master
    python -m apps.sets rec start --base-url URL --source loopback --device-name "BlackHole 2ch"
    python -m apps.sets rec status --base-url URL
    python -m apps.sets rec stop --base-url URL

A master start is pushed to the open /performance page, which connects its tap
before the start answers; `rec start` returns once audio is really written.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from typing import Any

from .master_mix import ATTACH_TIMEOUT_S
from .recorder_service import RECORD_SOURCES

_TIMEOUT_S = 30.0


class RecCommandFailed(RuntimeError):
    """The daemon refused or could not be reached; carries its reason."""


def _call(base_url: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        raise RecCommandFailed(f"{method} {path}: {exc.code} {exc.read().decode(errors='replace')}") from exc
    except urllib.error.URLError as exc:
        raise RecCommandFailed(f"{method} {path}: daemon at {base_url} unreachable: {exc.reason}") from exc


def start_body(source: str, device_name: str | None, sources: list[str]) -> dict[str, Any]:
    """The start request the picker would send for this source."""
    if (source in ("loopback", "external")) != (device_name is not None):
        raise RecCommandFailed(
            f"--source {source} {'needs' if device_name is None else 'takes no'} --device-name"
        )
    body: dict[str, Any] = {"session_id": None, "source": source, "sources": sources}
    if device_name is not None:
        body["device_name"] = device_name
    return body



def wait_until_recording(
    base_url: str,
    status: dict[str, Any],
    *,
    timeout_s: float = ATTACH_TIMEOUT_S + 2,
    poll_s: float = 0.2,
    read: Any = None,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> dict[str, Any]:
    """Return once the started capture is really writing (SET-12), or raise.

    A start answers while the first audio is still on its way (``starting``);
    the CLI, like the REC button, only reports a recording once it records.
    A capture that fails (no audio within the daemon's 10 s) raises with the
    daemon's reason."""
    read = read or (lambda: _call(base_url, "GET", "/api/sets/recorder"))
    deadline = monotonic() + timeout_s
    while status.get("capture") in ("starting", "waiting_permission"):
        if monotonic() > deadline:
            raise RecCommandFailed(f"the capture did not start within {timeout_s:g} s: {status}")
        sleep(poll_s)
        status = read()
    if status.get("capture") in ("failed", "stopped"):
        raise RecCommandFailed(f"the capture failed: {status.get('capture_error') or status}")
    return status

def dispatch(args: argparse.Namespace) -> int:
    if args.rec_cmd == "start":
        status = _call(
            args.base_url,
            "POST",
            "/api/sets/recorder/start",
            start_body(args.source, args.device_name, args.sources),
        )
        status = wait_until_recording(args.base_url, status)
    elif args.rec_cmd == "status":
        status = _call(args.base_url, "GET", "/api/sets/recorder")
    elif args.rec_cmd == "stop":
        current = _call(args.base_url, "GET", "/api/sets/recorder")
        if not current["active"]:
            raise RecCommandFailed("nothing is recording")
        status = _call(args.base_url, "POST", f"/api/sets/recorder/{current['session_id']}/stop")
    else:
        raise RecCommandFailed(f"unknown rec command {args.rec_cmd!r}")
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def add_parser(sub: Any) -> None:
    rec = sub.add_parser("rec", help="drive the running app's REC over HTTP")
    rec_sub = rec.add_subparsers(dest="rec_cmd", required=True)
    for name, text in (
        ("start", "start recording"),
        ("status", "print the recorder status"),
        ("stop", "stop the active recording"),
    ):
        cmd = rec_sub.add_parser(name, help=text)
        cmd.add_argument("--base-url", required=True, help="the app daemon, e.g. http://127.0.0.1:8702")
        if name == "start":
            cmd.add_argument("--source", required=True, choices=RECORD_SOURCES)
            cmd.add_argument("--device-name", default=None)
            cmd.add_argument(
                "--sources", nargs="*", default=["djay_monitor", "opendj_decks"],
                help="deck-state sources (the picker's are djay_monitor opendj_decks)",
            )
    rec.set_defaults(func=dispatch)


__all__ = ["RecCommandFailed", "add_parser", "dispatch", "start_body", "wait_until_recording"]
