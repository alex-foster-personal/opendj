"""Shell route navigation helpers for opendj CLI and MCP (issue #2866)."""
from __future__ import annotations

import time
from typing import Any

import httpx

from apps.opendj_cli.origin import EngineOrigin, unreachable

MIRROR_TIMEOUT_S = 10.0
SHELL_NAVIGATE_PATH = "/api/v1/shell/navigate"
MIRROR_PATH = "/api/v1/state/ui-mirror"
REMEDY_VERB = "opendj open performance"
MIRROR_POLL_S = 0.2
REQUEST_TIMEOUT_S = 5.0


def no_performance_page_message(origin: EngineOrigin) -> str:
    return (
        f"engine at {origin.base_url} has no performance page on record after "
        f"waiting {MIRROR_TIMEOUT_S:.0f}s; run `{REMEDY_VERB}` (or MCP open_route) "
        "so the desktop app navigates to /performance (409 client_open=false)"
    )


def _raise_no_performance_page(message: str) -> None:
    from apps.opendj_cli.client import NoPerformancePage

    raise NoPerformancePage(message)


def request_shell_navigate(origin: EngineOrigin, route: str = "/performance") -> dict[str, Any]:
    url = f"{origin.base_url}{SHELL_NAVIGATE_PATH}"
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            response = client.post(url, json={"route": route})
    except httpx.TransportError as error:
        raise unreachable(origin, error) from error
    if response.status_code == 422:
        _raise_no_performance_page(f"{response.status_code} from {url}: {response.text[:200]}")
    if response.status_code != 202:
        _raise_no_performance_page(f"{response.status_code} from {url}: {response.text[:200]}")
    payload = response.json()
    if not isinstance(payload, dict):
        _raise_no_performance_page(f"{url} answered 202 with a non-object body")
    return payload


def wait_for_performance_page(
    origin: EngineOrigin, timeout_s: float = MIRROR_TIMEOUT_S
) -> bool:
    deadline = time.monotonic() + timeout_s
    url = f"{origin.base_url}{MIRROR_PATH}"
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
                response = client.get(url)
        except httpx.TransportError:
            time.sleep(MIRROR_POLL_S)
            continue
        if response.status_code == 200:
            body = response.json()
            if isinstance(body, dict) and body.get("client_open") is True:
                return True
        time.sleep(MIRROR_POLL_S)
    return False


def ensure_performance_page(origin: EngineOrigin) -> bool:
    request_shell_navigate(origin, "/performance")
    return wait_for_performance_page(origin)


def open_shell_route(origin: EngineOrigin, route: str = "/performance") -> dict[str, Any]:
    request_shell_navigate(origin, route)
    if wait_for_performance_page(origin):
        return {"route": route, "client_open": True}
    _raise_no_performance_page(no_performance_page_message(origin))


def open_performance(origin: EngineOrigin) -> dict[str, Any]:
    return open_shell_route(origin, "/performance")
