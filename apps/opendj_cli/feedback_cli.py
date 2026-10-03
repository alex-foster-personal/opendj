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
    problem = summary_shape_problem(payload)
    if problem is not None:
        raise TypeError(f"GET {COMMENTS_SUMMARY_PATH} returned a malformed body: {problem}")
    return payload


_OPERATOR_KEYS = (
    "total",
    "sent_to_queue",
    "in_progress",
    "delegated",
    "fixed",
    "merged",
    "blocked",
    "harvested",
)
_LIFECYCLE_KEYS = (
    "total",
    "untriaged",
    "open",
    "issued",
    "blocked",
    "fixed",
    "merged",
    "harvested",
)
_FLEET_CORRELATIONS = frozenset({"ok", "ledger_missing", "ledger_unreadable"})


def summary_shape_problem(payload: object) -> str | None:
    """Why a 2xx summary body breaks CommentSummaryOut, or None when it fits.

    A malformed body (no operator, a missing or non-integer bucket, an unknown
    fleet_correlation) must fail as request_failed in both JSON and text mode,
    never print as a confirmed result (PR #4094 Sol P2).
    """
    if not isinstance(payload, dict):
        return "not a JSON object"
    for name, keys in (("operator", _OPERATOR_KEYS), ("lifecycle", _LIFECYCLE_KEYS)):
        buckets = payload.get(name)
        if not isinstance(buckets, dict):
            return f"{name} is missing"
        for key in keys:
            value = buckets.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return f"{name}.{key} is {value!r}"
    if payload.get("fleet_correlation") not in _FLEET_CORRELATIONS:
        return f"fleet_correlation is {payload.get('fleet_correlation')!r}"
    return None


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
        print(_format_operator(payload["operator"], payload["fleet_correlation"]))
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
