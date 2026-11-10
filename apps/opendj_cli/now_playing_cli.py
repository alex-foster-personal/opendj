"""``opendj now-playing``: the track the room hears (NOWP-11489, issue #10336).

    opendj now-playing [--json] [--watch] [--interval S]

``GET /api/v1/now-playing`` answers which one track the room hears after a 3 s settle delay.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

ROUTE = "/api/v1/now-playing"
REQUEST_TIMEOUT_S = 15.0
_sleep = time.sleep


class NowPlayingRequestFailed(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def fetch_now_playing(base_url: str) -> tuple[dict[str, Any], str]:
    """GET the route; returns ``(parsed, response.text)``. Raises on non-2xx."""
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        response = client.get(f"{base_url.rstrip('/')}{ROUTE}")
    if not response.is_success:
        raise NowPlayingRequestFailed(response.status_code, response.text)
    return response.json(), response.text


def format_line(doc: dict[str, Any]) -> str:
    track = doc.get("track")
    if not isinstance(track, dict):
        line = "nothing announced yet"
    else:
        artist = track.get("artist")
        title = track.get("title")
        stable_id = track.get("stable_id", "?")
        head = f"{artist} - {title}" if isinstance(artist, str) and isinstance(title, str) else stable_id
        if isinstance(artist, str) and not isinstance(title, str):
            head = f"{artist} - {stable_id}"
        if not isinstance(artist, str) and isinstance(title, str):
            head = title
        parts: list[str] = []
        camelot, musical = track.get("key_camelot"), track.get("key_musical")
        if isinstance(camelot, str) and isinstance(musical, str):
            parts.append(f"{camelot} / {musical}")
        elif isinstance(camelot, str):
            parts.append(camelot)
        elif isinstance(musical, str):
            parts.append(musical)
        bpm = track.get("bpm")
        if isinstance(bpm, (int, float)) and not isinstance(bpm, bool):
            parts.append(f"{float(bpm):.1f} BPM")
        deck = track.get("deck")
        if isinstance(deck, int):
            parts.append(f"deck {deck}")
        since = track.get("since")
        if isinstance(since, str):
            parts.append(f"since {since}")
        line = f"{head}  [{', '.join(parts)}]" if parts else head
    if doc.get("stale") is True:
        line = f"{line} (stale)"
    return line


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opendj now-playing",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=True,
    )
    parser.add_argument("--json", action="store_true", help="print the route JSON body verbatim")
    parser.add_argument("--watch", action="store_true", help="print one line whenever generation changes")
    parser.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds (default 1.0)")
    return parser


def _parse(rest: Sequence[str]) -> argparse.Namespace:
    parser = _parser()
    try:
        return parser.parse_args(list(rest))
    except SystemExit as error:
        raise UsageError("usage: opendj now-playing [--json] [--watch] [--interval S]") from error


def _emit(as_json: bool, doc: dict[str, Any], text: str) -> None:
    payload = text if as_json else format_line(doc)
    print(payload, flush=True)


def _run_once(base_url: str, as_json: bool) -> int:
    try:
        doc, text = fetch_now_playing(base_url)
    except NowPlayingRequestFailed as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    _emit(as_json, doc, text)
    return EXIT_CONFIRMED


def _run_watch(base_url: str, as_json: bool, interval: float) -> int:
    last_generation: int | None = None
    try:
        while True:
            try:
                doc, text = fetch_now_playing(base_url)
            except NowPlayingRequestFailed as error:
                return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
            except httpx.HTTPError as error:
                return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
            generation = doc.get("generation")
            if not isinstance(generation, int):
                return _fail(as_json, "request_failed", "now-playing answer missing generation", EXIT_FAILED)
            if last_generation is None or generation != last_generation:
                _emit(as_json, doc, text)
                last_generation = generation
            _sleep(interval)
    except KeyboardInterrupt:
        return EXIT_CONFIRMED


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    try:
        ns = _parse(rest)
    except UsageError as error:
        return _fail(as_json, "usage", str(error), EXIT_FAILED)
    as_json = as_json or ns.json
    if ns.interval <= 0:
        return _fail(as_json, "usage", "--interval must be > 0", EXIT_FAILED)

    try:
        target = resolve_backend_base_url(lock_path=lock)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)

    if not ns.watch:
        return _run_once(target.base_url, as_json)
    return _run_watch(target.base_url, as_json, ns.interval)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
