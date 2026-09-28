"""Trunk Merge Queue settings as code: ``ci/trunk-merge-queue.json`` is the desired state.

The JSON body is the Trunk ``updateQueue`` request, so the file is exactly what gets sent.
The queue's run state (running, paused, draining) is deliberately NOT part of it: pausing
the queue during an incident is an operational act, and a config push must never undo it.

    python -m scripts.trunk_queue show     # live settings as JSON
    python -m scripts.trunk_queue diff     # desired vs live; exit 1 on drift
    python -m scripts.trunk_queue apply    # push desired settings, then re-read and verify

``TRUNK_API_TOKEN`` must be set (org API token; Doppler general/dev_personal, repo secret).

Requirements:
  ✔︎ diff reports every setting whose live value differs from the file, and exits 1
    - [if] live concurrency is 5 and the file says 3 [then ⛔️ exit 0]
    - [if] allowedBotSubmitters differ only in order [then ⛔️ reported as drift]
    - [if] the live queue is paused and nothing else differs [then ⛔️ reported as drift]
  ✔︎ apply never sends ``state`` and proves the result by re-reading
    - [if] the desired file contains a ``state`` key [then ⛔️ accepted silently]
    - [if] the live read after apply still differs [then ⛔️ exit 0]
  ✔︎ a missing token or an HTTP error fails loudly, never as "no drift"
    - [if] TRUNK_API_TOKEN is unset [then ⛔️ any API call is attempted]
    - [if] Trunk answers 403 [then ⛔️ printed as a clean diff]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "ci" / "trunk-merge-queue.json"
API_BASE = "https://api.trunk.io/v1"
# Identity of the queue (the request's address, not a setting) and its run state.
ADDRESS_KEYS = frozenset({"repo", "targetBranch"})
OPERATIONAL_KEYS = frozenset({"state"})
# Lists whose order carries no meaning on Trunk's side.
UNORDERED_KEYS = frozenset({"allowedBotSubmitters", "requiredStatuses"})
# Success bodies that carry no data (measured Mon 28 Sep 2026: submitPullRequest returns `OK`).
PLAIN_SUCCESS_BODIES = frozenset({b"", b"OK"})


class TrunkApiError(RuntimeError):
    pass


# -----------------------------------------------------------------------------
def load_desired(path: Path = CONFIG_PATH) -> dict[str, Any]:
    desired = json.loads(path.read_text())
    operational = OPERATIONAL_KEYS & desired.keys()
    if operational:
        raise ValueError(
            f"{path} sets {sorted(operational)}: the queue's run state is operational, "
            "not configuration; pause or resume it with the Trunk UI or API instead"
        )
    return desired


def _comparable(key: str, value: Any) -> Any:
    if key in UNORDERED_KEYS and isinstance(value, list):
        return sorted(value)
    return value


def settings_drift(desired: dict[str, Any], live: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Every desired setting whose live value differs, as ``{key: (desired, live)}``."""
    drift: dict[str, tuple[Any, Any]] = {}
    for key, want in desired.items():
        if key in ADDRESS_KEYS:
            continue
        have = live.get(key, "<absent>")
        if _comparable(key, want) != _comparable(key, have):
            drift[key] = (want, have)
    return drift


# -----------------------------------------------------------------------------
def _token() -> str:
    token = os.environ.get("TRUNK_API_TOKEN", "").strip()
    if not token:
        raise TrunkApiError("TRUNK_API_TOKEN is not set; refusing to call the Trunk API")
    return token


def _post(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{API_BASE}/{endpoint}",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "music-dj-tools-trunk-queue/1",
            "x-api-token": _token(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().strip()
    except urllib.error.HTTPError as error:
        detail = error.read()[:300].decode(errors="replace")
        raise TrunkApiError(f"POST {endpoint} -> HTTP {error.code}: {detail}") from None
    # Write endpoints (submitPullRequest) answer a bare `OK`; any other body must be JSON.
    if raw in PLAIN_SUCCESS_BODIES:
        return {}
    return json.loads(raw)


def fetch_live(desired: dict[str, Any]) -> dict[str, Any]:
    return _post("getQueue", {key: desired[key] for key in ADDRESS_KEYS})


def _print_drift(drift: dict[str, tuple[Any, Any]]) -> None:
    for key, (want, have) in sorted(drift.items()):
        print(f"DRIFT {key}: desired={json.dumps(want)} live={json.dumps(have)}")


# -----------------------------------------------------------------------------
def command_show(desired: dict[str, Any]) -> int:
    print(json.dumps(fetch_live(desired), indent=2, sort_keys=True))
    return 0


def command_diff(desired: dict[str, Any]) -> int:
    live = fetch_live(desired)
    drift = settings_drift(desired, live)
    _print_drift(drift)
    checked = len(desired.keys() - ADDRESS_KEYS)
    if drift:
        print(f"[ERROR] {len(drift)} of {checked} settings drifted from {CONFIG_PATH.name}")
        return 1
    state = live.get("state")
    print(f"[OK] all {checked} settings match {CONFIG_PATH.name} (queue state: {state})")
    return 0


def command_apply(desired: dict[str, Any]) -> int:
    before = settings_drift(desired, fetch_live(desired))
    _print_drift(before)
    _post("updateQueue", desired)
    after = settings_drift(desired, fetch_live(desired))
    if after:
        _print_drift(after)
        print(f"[ERROR] {len(after)} settings still differ after apply")
        return 1
    print(f"[OK] applied; {len(before)} setting(s) changed, live now matches {CONFIG_PATH.name}")
    return 0


COMMANDS = {"show": command_show, "diff": command_diff, "apply": command_apply}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command](load_desired())
    except TrunkApiError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
