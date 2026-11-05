"""``opendj practice <device> [--target <control id>]``: open practice mode (ADR-0137).

Same route a person reaches by URL: ``<ui_url>/practice/<device>``. The device must ship a
panel layout, checked against ``GET /api/v1/practice/devices`` before the browser opens, so
an unknown device fails here with the list of known ones instead of opening an error page.
"""

from __future__ import annotations

import json
import urllib.parse
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.client import EngineClient
from apps.opendj_cli.origin import EngineIdentityMismatch, EngineNotRunning, resolve_verified_origin


class PracticeUsage(ValueError):
    pass


def practice_url(ui_url: str, device: str, target: str | None) -> str:
    url = f"{ui_url.rstrip('/')}/practice/{urllib.parse.quote(device)}"
    return f"{url}?{urllib.parse.urlencode({'target': target})}" if target else url


def parse_practice_args(rest: Sequence[str]) -> tuple[str, str | None]:
    tokens = list(rest)
    target: str | None = None
    if "--target" in tokens:
        i = tokens.index("--target")
        if i + 1 >= len(tokens):
            raise PracticeUsage("--target needs a control id")
        target = tokens[i + 1]
        del tokens[i : i + 2]
    if len(tokens) != 1:
        raise PracticeUsage("usage: opendj practice <device> [--target <control id>]")
    return tokens[0], target


def run(rest: Sequence[str], *, as_json: bool, lock: Path | None) -> int:
    as_json = as_json or "--json" in rest
    rest = [t for t in rest if t != "--json"]
    try:
        device, target = parse_practice_args(rest)
    except PracticeUsage as error:
        print(json.dumps({"error": "usage", "message": str(error)}) if as_json else f"[ERROR] {error}")
        return EXIT_FAILED
    try:
        client = EngineClient(origin=resolve_verified_origin(lock))
    except (EngineNotRunning, EngineIdentityMismatch) as error:
        print(json.dumps({"error": "engine_not_running", "message": str(error)}) if as_json else f"[ERROR] {error}")
        return EXIT_NO_ENGINE
    known = client.practice_devices()
    if device not in known:
        message = f"no practice layout for {device!r}; known: {', '.join(known)}"
        print(json.dumps({"error": "unknown_device", "message": message}) if as_json else f"[ERROR] {message}")
        return EXIT_FAILED
    url = practice_url(str(client.ui_address()["ui_url"]), device, target)
    if not webbrowser.open(url):
        print(
            json.dumps({"error": "browser_unavailable", "url": url})
            if as_json
            else f"[ERROR] no browser could open {url}"
        )
        return EXIT_FAILED
    print(json.dumps({"opened": True, "url": url}) if as_json else f"opened {url}")
    return EXIT_CONFIRMED
