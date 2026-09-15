"""The HTTP half of the CLI: two routes, and every response named.

``GET /api/v1/state/ui-mirror`` and ``POST /api/v1/commands`` are the whole
transport (AGENT-02 and AGENT-03). A 409 from either is not an error to
retry: it means no performance page is on record, so an order could only be
held forever. That distinction is stated, never smoothed over, because the
Web Audio engine is browser-owned and there is no headless path to fall back
to (``apps/webui/server/routes/commands.py:1-5``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from apps.opendj_cli.origin import EngineOrigin, unreachable

MIRROR_PATH = "/api/v1/state/ui-mirror"
COMMANDS_PATH = "/api/v1/commands"
UI_PREFS_PATH = "/api/v1/ui-prefs"

# A page claims an order within one poll tick (50 ms) and a ramp completes in
# its own declared duration, so this only fires on a wedged page or a wedged
# engine. It exists so the CLI cannot hang, and the timeout error says so.
ORDER_TIMEOUT_S = 60.0
MIRROR_TIMEOUT_S = 10.0


class NoPerformancePage(RuntimeError):
    """The engine is up and answered 409: no page is open to claim an order."""


class OrderRejected(RuntimeError):
    """The route refused the order body before any page could see it."""


class OrderTimedOut(RuntimeError):
    """The page accepted nothing before the deadline."""


class MalformedResult(RuntimeError):
    """The route answered 200 with a body that is not a result document."""


@dataclass
class EngineClient:
    """A thin, fail-fast client of one engine origin."""

    origin: EngineOrigin
    timeout_s: float = ORDER_TIMEOUT_S

    def mirror(self) -> dict[str, Any]:
        """The current UI mirror, or a refusal that says why there is none."""
        from apps.opendj_cli.shell_navigate import (
            ensure_performance_page,
            no_performance_page_message,
        )

        response = self._request("GET", MIRROR_PATH, timeout=MIRROR_TIMEOUT_S)
        if response.status_code == 409:
            if ensure_performance_page(self.origin):
                response = self._request("GET", MIRROR_PATH, timeout=MIRROR_TIMEOUT_S)
                if response.status_code == 200:
                    return self._document(response)
            raise NoPerformancePage(no_performance_page_message(self.origin))
        self._require_ok(response)
        return self._document(response)

    def ui_prefs(self) -> dict[str, Any]:
        """Disk-backed ui prefs (includes persisted master_muted)."""
        response = self._request("GET", UI_PREFS_PATH, timeout=MIRROR_TIMEOUT_S)
        self._require_ok(response)
        return self._document(response)

    def post_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """Submit one AGENT-03 order and wait for the page's own result."""
        from apps.opendj_cli.shell_navigate import (
            ensure_performance_page,
            no_performance_page_message,
        )

        response = self._request(
            "POST", COMMANDS_PATH, json=order, timeout=self.timeout_s
        )
        if response.status_code == 409:
            if ensure_performance_page(self.origin):
                response = self._request(
                    "POST", COMMANDS_PATH, json=order, timeout=self.timeout_s
                )
                if response.status_code != 409:
                    if response.status_code == 422:
                        raise OrderRejected(self._detail(response))
                    self._require_ok(response)
                    return self._document(response)
            raise NoPerformancePage(no_performance_page_message(self.origin))
        if response.status_code == 422:
            raise OrderRejected(self._detail(response))
        self._require_ok(response)
        return self._document(response)

    # ----- internals ----------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.origin.base_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                return client.request(method, url, **kwargs)
        except httpx.TimeoutException as error:
            raise OrderTimedOut(
                f"{url} did not answer within {kwargs.get('timeout', self.timeout_s)}s"
            ) from error
        except httpx.TransportError as error:
            raise unreachable(self.origin, error) from error

    def _require_ok(self, response: httpx.Response) -> None:
        if response.status_code == 200:
            return
        raise OrderRejected(
            f"{response.status_code} from {response.request.url}: "
            f"{self._detail(response)}"
        )

    def _document(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise OrderRejected(
                f"{response.request.url} answered 200 with a body that is not JSON: "
                f"{response.text[:200]!r}"
            ) from error
        if not isinstance(payload, dict):
            raise OrderRejected(
                f"{response.request.url} answered 200 with a {type(payload).__name__}, "
                "not the documented object"
            )
        return payload

    def _detail(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text[:200]
        if isinstance(payload, dict) and "detail" in payload:
            return str(payload["detail"])
        return str(payload)[:200]
