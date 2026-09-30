"""``opendj feedback comments summary``: FB-20 HTTP parity (issue #4085, pin 6af63c5e9b7c)."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning

COMMENTS_SUMMARY_PATH = "/api/v1/feedback/comments/summary"
REQUEST_TIMEOUT_S = 60.0

_USAGE = "usage: opendj feedback comments summary [--json]"


def _fail(as_json: bool, code: str, message: str, exit_code: int) -> int:
    if as_json:
        print(
            json.dumps(
                {"error": {"code": code, "message": message}, "exit_code": exit_code},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"opendj: {message}", file=sys.stderr)
    return exit_code


def _fetch_summary(*, lock: Path | None) -> dict[str, Any]:
    target = resolve_backend_base_url(lock_path=lock)
    url = f"{target.base_url.rstrip('/')}{COMMENTS_SUMMARY_PATH}"
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        response = client.get(url, headers={"Accept": "application/json"})
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(
            f"GET {COMMENTS_SUMMARY_PATH} returned {response.status_code}: {response.text}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError(f"GET {COMMENTS_SUMMARY_PATH} returned non-object JSON")
    return payload


def run_comments_summary(
    rest: Sequence[str],
    *,
    as_json: bool = False,
    lock: Path | None = None,
) -> int:
    if rest:
        return _fail(
            as_json,
            "usage",
            f"feedback comments summary takes no arguments, got {rest[0]!r}; {_USAGE}",
            EXIT_FAILED,
        )
    try:
        payload = _fetch_summary(lock=lock)
    except UsageError as error:
        return _fail(as_json, "usage", str(error), EXIT_FAILED)
    except EngineNotRunning as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except (httpx.HTTPError, RuntimeError, TypeError, json.JSONDecodeError) as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        operator = payload.get("operator")
        if isinstance(operator, dict):
            print(_format_operator(operator, payload.get("fleet_correlation")))
        else:
            print(json.dumps(payload, indent=2, sort_keys=True))
    return EXIT_CONFIRMED


def _format_operator(operator: dict[str, Any], fleet_correlation: object) -> str:
    """Text rendering of the operator buckets, in the UI hover's order.

    blocked and harvested print only when nonzero (as on the hover), so the
    buckets shown always account for every pin in total.
    """
    keys = ["total", "sent_to_queue", "in_progress", "delegated", "fixed", "merged"]
    keys += [k for k in ("blocked", "harvested") if operator.get(k)]
    lines = [f"{key}: {operator.get(key)}" for key in keys]
    lines.append(f"fleet_correlation: {fleet_correlation}")
    return "\n".join(lines)


def run(
    rest: Sequence[str],
    *,
    as_json: bool = False,
    lock: Path | None = None,
) -> int:
    if not rest:
        return _fail(as_json, "usage", f"feedback requires a subcommand; {_USAGE}", EXIT_FAILED)
    head, *tail = rest
    if head == "comments" and tail == ["summary"]:
        return run_comments_summary((), as_json=as_json, lock=lock)
    if head == "comments" and not tail:
        return _fail(
            as_json,
            "usage",
            f"feedback comments requires a subcommand; {_USAGE}",
            EXIT_FAILED,
        )
    return _fail(as_json, "usage", f"unknown feedback subcommand: {head!r}; {_USAGE}", EXIT_FAILED)
