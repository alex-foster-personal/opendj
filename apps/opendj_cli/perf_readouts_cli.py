"""``opendj perf_readouts``: the top-bar performance card rows (PERFREADOUT-12579).

Read-only. The rows are the page's own ``perfReadoutRows()`` (name, value,
explainer, detail), posted with the browser's client performance sample and
served back by ``GET /api/v1/performance/telemetry/readouts``. The MCP tool
``perf_readouts`` calls :func:`read_perf_readouts` too, so both surfaces return
the same serialized rows. Neither surface writes the telemetry sink.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail as _fail

PERF_READOUTS_PATH = "/api/v1/performance/telemetry/readouts"
REQUEST_TIMEOUT_S = 3.0


class PerfReadoutsUnavailable(RuntimeError):
    """The engine answered without rows (404 before any page posted them, or an error)."""

    def __init__(self, status_code: int, detail: object) -> None:
        """Keep the engine's status and detail for the caller's refusal."""
        super().__init__(f"GET {PERF_READOUTS_PATH} returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def read_perf_readouts(base_url: str) -> dict[str, Any]:
    """GET the card rows from the engine at ``base_url``; the one read both surfaces share."""
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        response = client.get(f"{base_url.rstrip('/')}{PERF_READOUTS_PATH}")
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:500]
    if not response.is_success:
        detail = body.get("detail") if isinstance(body, dict) else body
        raise PerfReadoutsUnavailable(response.status_code, detail)
    if not isinstance(body, dict) or not isinstance(body.get("rows"), list):
        raise PerfReadoutsUnavailable(response.status_code, "response carries no rows list")
    return body


def run(rest: Sequence[str], *, as_json: bool = False, lock: Path | None = None) -> int:
    """Print the card rows, or refuse with a clear reason when none were posted."""
    if rest:
        return _fail(as_json, "usage", f"perf_readouts takes no arguments, got {rest[0]!r}", EXIT_FAILED)
    try:
        target = resolve_backend_base_url(lock_path=lock)
    except (UsageError, EngineNotRunning) as error:
        return _fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    try:
        payload = read_perf_readouts(target.base_url)
    except PerfReadoutsUnavailable as error:
        code = "no_readouts" if error.status_code == 404 else "request_failed"
        return _fail(as_json, code, str(error.detail), EXIT_FAILED)
    except httpx.HTTPError as error:
        return _fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for row in payload["rows"]:
            print(f"{row.get('name')}: {row.get('value')} -- {row.get('explainer')}")
    return EXIT_CONFIRMED
