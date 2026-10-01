"""``python -m apps.stems cache-status | cache-enforce | cache-settings`` (STEM-43).

Agent-native parity for the stem cache disk budget. These verbs call the
RUNNING ENGINE over HTTP rather than acting on the data directory in-process,
and that is deliberate: which bundles are loaded or playing is known only to
the engine (``OPEN_DECKS`` is in-memory and per-process). A second process
enforcing the budget on its own would see no decks at all and could evict the
bundle under a playing deck.

Engine address: ``MDT_ENGINE_URL`` (same variable the other engine-backed
stems verbs read), or ``--engine-url``.

* [if] the engine is not reachable [then ⛔️] the verb exits non-zero naming
  the URL it tried; it never falls back to acting without the engine.
* [if] ``cache-status`` reports ``low_disk`` and ``--fail-on-low-disk`` is
  set [then] the exit code is 3, so a script can gate on it.

-Claude
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from typing import Any

ENGINE_URL_ENV: str = "MDT_ENGINE_URL"
DEFAULT_ENGINE_URL: str = "http://127.0.0.1:9400"
STATUS_PATH: str = "/api/v1/stems/cache/status"
ENFORCE_PATH: str = "/api/v1/stems/cache/enforce"
SETTINGS_PATH: str = "/api/v1/stems/cache/settings"
EXIT_LOW_DISK: int = 3
REQUEST_TIMEOUT_S: float = 600.0


def _engine_request(
    engine_url: str, method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    url = f"{engine_url.rstrip('/')}{path}"
    request = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"error: engine {method} {url} failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"error: engine not reachable at {url}: {exc.reason}. Set {ENGINE_URL_ENV} "
            "or pass --engine-url; this verb never acts without the engine because "
            "only the engine knows which bundles are on a deck."
        ) from exc


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_cache_status(args: argparse.Namespace) -> int:
    """Mirrors ``GET /api/v1/stems/cache/status``."""
    payload = _engine_request(args.engine_url, "GET", STATUS_PATH)
    _print(payload)
    if args.fail_on_low_disk and payload["state"] == "low_disk":
        return EXIT_LOW_DISK
    return 0


def cmd_cache_enforce(args: argparse.Namespace) -> int:
    """Mirrors ``POST /api/v1/stems/cache/enforce``."""
    _print(_engine_request(args.engine_url, "POST", ENFORCE_PATH, {"dry_run": args.dry_run}))
    return 0


def cmd_cache_settings(args: argparse.Namespace) -> int:
    """Mirrors ``GET`` / ``PUT /api/v1/stems/cache/settings``: with no
    override flag it reads, with any it writes that partial update."""
    overrides: dict[str, Any] = {
        key: value
        for key, value in (
            ("floor_gib", args.floor_gib),
            ("floor_fraction", args.floor_fraction),
            ("max_cache_gib", args.max_cache_gib),
            ("enforce_interval_s", args.interval_s),
            ("auto_evict", {"on": True, "off": False}.get(args.auto_evict)),
        )
        if value is not None
    }
    if args.clear_max_cache_gib:
        overrides["clear_max_cache_gib"] = True
    if overrides:
        _print(_engine_request(args.engine_url, "PUT", SETTINGS_PATH, overrides))
    else:
        _print(_engine_request(args.engine_url, "GET", SETTINGS_PATH))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    """Add the three cache verbs to the ``apps.stems`` parser."""
    engine = argparse.ArgumentParser(add_help=False)
    engine.add_argument(
        "--engine-url",
        default=os.environ.get(ENGINE_URL_ENV, DEFAULT_ENGINE_URL),
        help=f"running engine base URL (default ${ENGINE_URL_ENV} or {DEFAULT_ENGINE_URL})",
    )

    status = sub.add_parser(
        "cache-status", parents=[engine],
        help="stem cache vs the free-disk floor: state, shortfall, local-only bundles",
    )
    status.add_argument(
        "--fail-on-low-disk", action="store_true",
        help=f"exit {EXIT_LOW_DISK} when the state is low_disk",
    )
    status.set_defaults(func=cmd_cache_status)

    enforce = sub.add_parser(
        "cache-enforce", parents=[engine],
        help="evict R2-confirmed LRU bundles down to the free-disk floor, now",
    )
    enforce.add_argument("--dry-run", action="store_true", help="report the plan, remove nothing")
    enforce.set_defaults(func=cmd_cache_enforce)

    settings = sub.add_parser(
        "cache-settings", parents=[engine],
        help="read the stem cache settings, or override any of them",
    )
    settings.add_argument("--floor-gib", type=float, default=None)
    settings.add_argument("--floor-fraction", type=float, default=None)
    settings.add_argument("--max-cache-gib", type=float, default=None)
    settings.add_argument("--clear-max-cache-gib", action="store_true")
    settings.add_argument("--interval-s", type=float, default=None)
    settings.add_argument("--auto-evict", choices=("on", "off"), default=None)
    settings.set_defaults(func=cmd_cache_settings)


__all__ = [
    "EXIT_LOW_DISK",
    "cmd_cache_enforce",
    "cmd_cache_settings",
    "cmd_cache_status",
    "register",
]
