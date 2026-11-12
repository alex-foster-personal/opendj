"""``python -m apps.sets rec``: drive the RUNNING app's REC over HTTP (SET-12).

The in-process ``start`` command runs its own recorder and can never record
the master mix, which only the app's page can tap. ``rec`` is the REC
picker's agent twin: it calls the same routes the picker calls, with the same
explicit ``--source``, so a script and the button cannot disagree.

    python -m apps.sets rec start --base-url http://127.0.0.1:8702 --source master
    python -m apps.sets rec start --base-url URL --source loopback --device-name "BlackHole 2ch"
    python -m apps.sets rec status --base-url URL
    python -m apps.sets rec remembered-input --base-url URL
    python -m apps.sets rec stop --base-url URL
    python -m apps.sets rec observations --base-url URL
    python -m apps.sets rec list --base-url URL
    python -m apps.sets rec sessions --base-url URL
    python -m apps.sets rec timeline --base-url URL --session-id SESSION
    python -m apps.sets rec transitions --base-url URL --session-id SESSION
    python -m apps.sets rec recover --base-url URL
    python -m apps.sets rec share get --base-url URL --session-id SESSION
    python -m apps.sets rec share publish --base-url URL --session-id SESSION
    python -m apps.sets rec split SESSION_ID --base-url URL [--out DIR]

A master start is pushed to the open /performance page, which connects its tap
before the start answers; `rec start` returns once audio is really written.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .master_mix import ATTACH_TIMEOUT_S
from .recorder_service import RECORD_SOURCES, recorded_duration_within, session_wav_duration_s

_TIMEOUT_S = 30.0


class RecCommandFailed(RuntimeError):
    """The daemon refused or could not be reached; carries its reason."""


def _call(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    parse_json: bool = True,
) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            payload = response.read()
            if not parse_json:
                return payload
            return json.loads(payload or b"null")
    except urllib.error.HTTPError as exc:
        raise RecCommandFailed(f"{method} {path}: {exc.code} {exc.read().decode(errors='replace')}") from exc
    except urllib.error.URLError as exc:
        raise RecCommandFailed(f"{method} {path}: daemon at {base_url} unreachable: {exc.reason}") from exc


def start_body(source: str, device_name: str | None, sources: list[str]) -> dict[str, Any]:
    """The start request the picker would send for this source."""
    if (source in ("loopback", "external")) != (device_name is not None):
        raise RecCommandFailed(f"--source {source} {'needs' if device_name is None else 'takes no'} --device-name")
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


def session_dir(status: Mapping[str, Any]) -> Path:
    """The directory that holds a recording's WAV segments."""
    session_id = status.get("session_id")
    root = status.get("recordings_dir")
    if not isinstance(session_id, str) or not session_id:
        raise RecCommandFailed("the recorder status has no session_id")
    if not isinstance(root, str) or not root:
        raise RecCommandFailed("the recorder status has no recordings_dir")
    return Path(root) / session_id


def verify_recorded_duration(
    status: Mapping[str, Any],
    *,
    expected_s: float,
    tolerance_s: float = 0.1,
) -> float:
    """Return the measured WAV duration or raise when outside ``tolerance_s``."""
    measured = session_wav_duration_s(session_dir(status))
    if not recorded_duration_within(session_dir(status), expected_s=expected_s, tolerance_s=tolerance_s):
        raise RecCommandFailed(f"recorded {measured:.3f} s of audio, expected {expected_s:.3f} s ± {tolerance_s:g} s")
    return measured


def _session_id_for_stop(base_url: str, session_id: str | None) -> str:
    if session_id is not None:
        return session_id
    current = _call(base_url, "GET", "/api/sets/recorder")
    if not current["active"]:
        raise RecCommandFailed("nothing is recording")
    return current["session_id"]


def _validate_timeline_ndjson(payload: bytes) -> None:
    """Reject timeline bodies agents cannot replay line-by-line (stricter than HTTP tail tolerance)."""
    if not payload:
        return
    text = payload.decode("utf-8")
    line_no = 0
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        line_no += 1
        try:
            json.loads(raw)
        except json.JSONDecodeError as exc:
            snippet = raw[:80] + ("..." if len(raw) > 80 else "")
            raise RecCommandFailed(
                f"malformed timeline NDJSON at line {line_no}: {exc.msg}; snippet: {snippet!r}"
            ) from exc


def _unknown_rec_command(rec_cmd: str) -> None:
    raise RecCommandFailed(f"unknown rec command {rec_cmd!r}")


def recover(base_url: str) -> dict[str, Any]:
    """Recover only the stale session and PID reported by the running daemon."""
    current = _call(base_url, "GET", "/api/sets/recorder")
    if not current["active"]:
        raise RecCommandFailed("nothing to recover")
    if current.get("recoverable") is not True:
        raise RecCommandFailed(
            f"recorder session {current.get('session_id')} owner PID {current.get('pid')} "
            "is still active; cannot recover"
        )
    session_id, pid = current.get("session_id"), current.get("pid")
    if not isinstance(session_id, str) or not session_id.strip() or type(pid) is not int or pid <= 0:
        raise RecCommandFailed(f"invalid recoverable recorder identity: session_id={session_id!r}, pid={pid!r}")
    return _call(base_url, "POST", f"/api/sets/recorder/{session_id}/recover", {"expected_pid": pid})


def _unknown_share_command(share_cmd: str) -> None:
    raise RecCommandFailed(f"unknown rec share command {share_cmd!r}")


def _split(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    if args.out is not None:
        body["out_dir"] = str(Path(args.out).expanduser().resolve())
    session_id = urllib.parse.quote(args.session_id, safe="")
    payload = _call(
        args.base_url,
        "POST",
        f"/api/sets/{session_id}/split",
        body or {},
    )
    if not isinstance(payload, dict):
        raise RecCommandFailed(f"unexpected split response type: {type(payload).__name__}")
    for entry in payload.get("track_files", []):
        print(f"{entry['path']}\t{entry['start_t_s']:.3f}\t{entry['duration_s']:.3f}\t{entry['size_bytes']}")
    return 0


def dispatch(args: argparse.Namespace) -> int:
    if args.rec_cmd == "split":
        return _split(args)
    try:
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
        elif args.rec_cmd == "observations":
            status = _call(args.base_url, "GET", "/api/sets/deck-observations")
        elif args.rec_cmd == "remembered-input":
            status = _call(args.base_url, "GET", "/api/sets/recorder/remembered-input")
        elif args.rec_cmd == "stop":
            session_id = _session_id_for_stop(args.base_url, args.session_id)
            status = _call(args.base_url, "POST", f"/api/sets/recorder/{session_id}/stop")
        elif args.rec_cmd in ("list", "sessions"):
            status = _call(args.base_url, "GET", "/api/sets")
        elif args.rec_cmd == "timeline":
            payload = _call(
                args.base_url,
                "GET",
                f"/api/sets/{args.session_id}/timeline",
                parse_json=False,
            )
            _validate_timeline_ndjson(payload)
            sys.stdout.buffer.write(payload)
            return 0
        elif args.rec_cmd == "transitions":
            payload = _call(
                args.base_url,
                "GET",
                f"/api/sets/{args.session_id}/transitions",
                parse_json=False,
            )
            sys.stdout.buffer.write(payload)
            return 0
        elif args.rec_cmd == "recover":
            status = recover(args.base_url)
        elif args.rec_cmd == "share":
            path = f"/api/sets/{args.session_id}/share"
            if args.share_cmd == "get":
                status = _call(args.base_url, "GET", path)
            elif args.share_cmd == "publish":
                status = _call(
                    args.base_url,
                    "POST",
                    path,
                    {"confirm_metadata_only": True},
                )
            else:
                _unknown_share_command(args.share_cmd)
        else:
            _unknown_rec_command(args.rec_cmd)
    except RecCommandFailed as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def add_parser(sub: Any) -> None:
    rec = sub.add_parser("rec", help="drive the running app's REC over HTTP")
    rec_sub = rec.add_subparsers(dest="rec_cmd", required=True)
    for name, text in (
        ("start", "start recording"),
        ("status", "print the recorder status"),
        ("observations", "print Open DJ deck-observation status"),
        ("remembered-input", "print the last REC input choice"),
        ("stop", "stop the active recording"),
        ("list", "list recorded sessions"),
        ("sessions", "list recorded sessions"),
        ("timeline", "print session timeline NDJSON"),
        ("transitions", "print session transitions JSON array"),
        ("recover", "recover a stale external recorder PID"),
        ("split", "cut the session recording into one WAV per track"),
    ):
        cmd = rec_sub.add_parser(name, help=text)
        cmd.add_argument("--base-url", required=True, help="the app daemon, e.g. http://127.0.0.1:8702")
        if name == "split":
            cmd.add_argument("session_id", help="recorded session id")
            cmd.add_argument("--out", default=None, help="absolute output directory")
        elif name == "start":
            cmd.add_argument("--source", required=True, choices=RECORD_SOURCES)
            cmd.add_argument("--device-name", default=None)
            cmd.add_argument(
                "--sources",
                nargs="*",
                default=["djay_monitor", "opendj_decks"],
                help="deck-state sources (the picker's are djay_monitor opendj_decks)",
            )
        elif name == "stop":
            cmd.add_argument(
                "--session-id",
                default=None,
                help="stop this session id (skip idle guard; daemon validates ownership)",
            )
        elif name in ("timeline", "transitions"):
            cmd.add_argument("--session-id", required=True)
    share = rec_sub.add_parser("share", help="read or publish metadata-only share links (SET-08)")
    share_sub = share.add_subparsers(dest="share_cmd", required=True)
    for share_name, share_text in (
        ("get", "print the metadata share JSON for a published set"),
        ("publish", "publish metadata-only sharing for a finalized set"),
    ):
        share_cmd = share_sub.add_parser(share_name, help=share_text)
        share_cmd.add_argument("--base-url", required=True, help="the app daemon, e.g. http://127.0.0.1:8702")
        share_cmd.add_argument("--session-id", required=True)
    rec.set_defaults(func=dispatch)


__all__ = [
    "RecCommandFailed",
    "add_parser",
    "dispatch",
    "recover",
    "session_dir",
    "start_body",
    "verify_recorded_duration",
    "wait_until_recording",
]
