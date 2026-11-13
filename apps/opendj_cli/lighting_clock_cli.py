"""Query and select the daemon's persisted lighting beat and phrase clock."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

import httpx

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, EXIT_NO_ENGINE
from apps.opendj_cli.api_cli import UsageError, resolve_backend_base_url
from apps.opendj_cli.origin import EngineNotRunning
from apps.opendj_cli.refusal import fail

CLOCK_PATH = "/api/v1/lighting/clock"
REQUEST_TIMEOUT_S = 10.0
CLOCK_SOURCES = frozenset({"master", "deck-1", "deck-2", "deck-3", "deck-4"})


class ClockState(TypedDict):
    clock_source: str
    effective_deck: str | None
    unavailable_reason: str | None


class _ClockRefusal(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _resolved_clock_fields(payload: dict[str, object]) -> tuple[str | None, str | None]:
    if "effective_deck" not in payload or "unavailable_reason" not in payload:
        raise TypeError("lighting clock response is missing resolution fields")
    deck = payload["effective_deck"]
    reason = payload["unavailable_reason"]
    if deck is not None and (not isinstance(deck, str) or deck not in {"1", "2", "3", "4"}):
        raise TypeError("lighting clock response has invalid effective_deck")
    if reason is not None and not isinstance(reason, str):
        raise TypeError("lighting clock response has invalid unavailable_reason")
    if (deck is None and not reason) or (deck is not None and reason is not None):
        raise TypeError("lighting clock response must explain an unavailable deck")
    return deck, reason


def _clock_state(payload: object) -> ClockState:
    if not isinstance(payload, dict):
        raise TypeError("lighting clock response must be an object")
    source = payload.get("clock_source")
    if not isinstance(source, str) or source not in CLOCK_SOURCES:
        raise TypeError("lighting clock response has invalid clock_source")
    deck, reason = _resolved_clock_fields(payload)
    if deck is not None and source not in {"master", f"deck-{deck}"}:
        raise TypeError("lighting clock response deck disagrees with clock_source")
    return {"clock_source": source, "effective_deck": deck, "unavailable_reason": reason}


def _request_clock(source: str | None, *, lock: Path | None) -> ClockState:
    target = resolve_backend_base_url(lock_path=lock)
    url = f"{target.base_url.rstrip('/')}{CLOCK_PATH}"
    method = "GET" if source is None else "PUT"
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        response = client.request(
            method,
            url,
            headers={"Accept": "application/json"},
            json={"clock_source": source} if source is not None else None,
        )
    if not 200 <= response.status_code < 300:
        code = "request_failed"
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = None
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            code = detail["code"]
        raise _ClockRefusal(code, f"{method} {CLOCK_PATH} returned {response.status_code}: {response.text}")
    return _clock_state(response.json())


def run(source: str | None, *, as_json: bool = False, lock: Path | None = None) -> int:
    """Use REST for both queries and updates; a refusal never confirms a change."""
    try:
        state = _request_clock(source, lock=lock)
    except UsageError as error:
        return fail(as_json, "usage", str(error), EXIT_FAILED)
    except EngineNotRunning as error:
        return fail(as_json, "engine_not_running", str(error), EXIT_NO_ENGINE)
    except _ClockRefusal as error:
        return fail(as_json, error.code, str(error), EXIT_FAILED)
    except (httpx.HTTPError, TypeError, json.JSONDecodeError) as error:
        return fail(as_json, "request_failed", str(error), EXIT_FAILED)
    if as_json:
        print(json.dumps(state, indent=2, sort_keys=True))
    elif state["effective_deck"] is None:
        print(f"lighting clock {state['clock_source']}: unavailable ({state['unavailable_reason']})")
    else:
        print(f"lighting clock {state['clock_source']}: deck {state['effective_deck']}")
    return EXIT_CONFIRMED
