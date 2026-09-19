"""The JSON transport a spoke talks to its hub over.

Split out of :mod:`apps.sync_hub.client` so that module is about the round
trip and this one is about moving bytes.

Transport is a two-method protocol rather than a hard httpx dependency, for
the same reason ``apps.cloud.lock`` takes an :class:`~apps.cloud.lock.S3Client`
protocol: the test hub is a Starlette ``TestClient``, the real hub is a URL
over the tailnet, and neither one is a mock of the other. The default
implementation is stdlib ``urllib`` so importing this costs nothing beyond
the standard library.

``PUSH_TIMEOUT_S`` is longer than ``DEFAULT_TIMEOUT_S`` because a single push
batch can take tens of seconds on a large library, and Tailscale serve may
close idle proxy connections around the same horizon. Hello, pull and digest
keep the shorter default.
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

API_PREFIX: str = "/api/v1/sync"
DEFAULT_TIMEOUT_S: float = 30.0
#: Per ``POST /push`` batch. Longer than hello/digest because one batch can
#: hold hundreds of rows and hub apply is not instant on a large library.
PUSH_TIMEOUT_S: float = 120.0


class SyncTransportError(RuntimeError):
    """The hub was unreachable or answered with something unusable.

    ``status_code`` and ``code`` are set only when the hub ANSWERED with a
    non-2xx: the HTTP status and the ``detail.code`` of its JSON body. Both
    stay None for an unreachable hub or a body with no code, which a caller
    must read as "not a declared refusal", never as any particular one.
    """

    def __init__(
        self, message: str, *, status_code: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _detail_code(body: str) -> str | None:
    """The ``detail.code`` FastAPI wraps a declared refusal in, when present."""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, str) else None


def refused(
    label: str, status_code: int, body: str, *, elapsed_s: float | None = None
) -> SyncTransportError:
    """The error for a non-2xx answer. Shared by every :class:`HubTransport`."""
    suffix = f" (after {elapsed_s:.1f}s)" if elapsed_s is not None else ""
    return SyncTransportError(
        f"{label} -> HTTP {status_code}: {body}{suffix}",
        status_code=status_code,
        code=_detail_code(body),
    )


@dataclass(frozen=True)
class TransportFailure:
    """Classified transport failure for scheduler backoff and status copy."""

    kind: Literal[
        "hub_error_5xx",
        "client_timeout",
        "proxy_or_hub_timeout",
        "unreachable",
    ]
    elapsed_s: float | None
    headline: str


_ELAPSED_RE = re.compile(r"after (\d+(?:\.\d+)?)s", re.IGNORECASE)


def _elapsed_from_message(message: str) -> float | None:
    match = _ELAPSED_RE.search(message)
    if match is None:
        return None
    return float(match.group(1))


def _timed_transport_failure(
    kind: str,
    elapsed: float | None,
    *,
    with_elapsed: str,
    without_elapsed: str,
) -> TransportFailure:
    if elapsed is not None:
        return TransportFailure(kind, elapsed, with_elapsed.format(elapsed=elapsed))
    return TransportFailure(kind, None, without_elapsed)


def _classify_http_failure(lower: str, elapsed: float | None) -> TransportFailure | None:
    if "http 502" in lower:
        return _timed_transport_failure(
            "proxy_or_hub_timeout",
            elapsed,
            with_elapsed="the hub or proxy closed the connection (502, after {elapsed:.0f}s)",
            without_elapsed=(
                "the hub or proxy closed the connection "
                "(502; may be Tailscale serve or client timeout)"
            ),
        )
    if re.search(r"http 5\d\d", lower):
        code = re.search(r"http (\d{3})", lower)
        status = int(code.group(1)) if code else 500
        return _timed_transport_failure(
            "hub_error_5xx",
            elapsed,
            with_elapsed=f"the hub answered with HTTP {status} (after {{elapsed:.0f}}s)",
            without_elapsed=f"the hub answered with HTTP {status}",
        )
    return None


def classify_transport_failure(message: str) -> TransportFailure | None:
    """Return a classified transport failure, or None when not transport-related."""
    lower = message.lower()
    if "syncdigestmismatch" in lower:
        return None
    elapsed = _elapsed_from_message(message)
    http_failure = _classify_http_failure(lower, elapsed)
    if http_failure is not None:
        return http_failure
    if "client timeout after" in lower or "timed out" in lower or "timeout" in lower:
        return _timed_transport_failure(
            "client_timeout",
            elapsed,
            with_elapsed="the hub did not respond in time (timeout after {elapsed:.0f}s)",
            without_elapsed="the hub did not respond in time (timeout)",
        )
    if (
        "connection refused" in lower
        or "failed:" in lower
        or "name or service not known" in lower
        or "getaddrinfo" in lower
    ):
        if "http " in lower:
            return None
        return TransportFailure(
            "unreachable",
            elapsed,
            "could not reach the hub machine",
        )
    return None


def is_timeout_transport(exc: SyncTransportError) -> bool:
    text = str(exc).lower()
    return "client timeout after" in text or "timed out" in text or "timeout" in text


def _url_error_is_timeout(exc: urllib.error.URLError) -> bool:
    reason = exc.reason
    if isinstance(reason, TimeoutError):
        return True
    if isinstance(reason, socket.timeout):
        return True
    return "timed out" in str(reason).lower()


def is_unreachable(exc: SyncTransportError) -> bool:
    """True when the hub never answered (connection refused, DNS, timeout)."""
    return exc.status_code is None


def is_hub_server_error(exc: SyncTransportError) -> bool:
    """True when the hub answered with HTTP 5xx."""
    return exc.status_code is not None and exc.status_code >= 500


# ----- transport -----------------------------------------------------------


class HubTransport(Protocol):
    """The two calls the spoke makes against a hub.

    Implementations:
      * production: :class:`HttpTransport` over the tailnet.
      * tests: a thin wrapper around Starlette's ``TestClient``.
    """

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """POST JSON to ``path``; return the decoded JSON body."""

    def get(
        self, path: str, params: Mapping[str, str | Sequence[str]]
    ) -> dict[str, Any]:
        """GET ``path`` with query ``params``; return the decoded JSON body."""


class HttpTransport:
    """stdlib-only JSON transport. Any non-2xx is an error, never a default."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        push_timeout_s: float = PUSH_TIMEOUT_S,
        bearer: str | None = None,
    ) -> None:
        """``bearer`` is this machine's sync credential
        (:mod:`apps.sync_hub.spoke_credential`), sent as ``Authorization:
        Bearer`` on every call. None sends no header, which an OBSERVE hub
        accepts and reports as ``missing``."""
        if not base_url:
            raise SyncTransportError("hub_url is empty")
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._push_timeout_s = push_timeout_s
        self._auth: dict[str, str] = (
            {} if bearer is None else {"Authorization": f"Bearer {bearer}"}
        )

    def _url(
        self, path: str, params: Mapping[str, str | Sequence[str]] | None = None
    ) -> str:
        url = f"{self._base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(dict(params), doseq=True)}"
        return url

    def _send(
        self, request: urllib.request.Request, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        effective = timeout_s if timeout_s is not None else self._timeout_s
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=effective) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            elapsed = time.monotonic() - started
            detail = exc.read().decode("utf-8", errors="replace")
            raise refused(
                f"{request.get_method()} {request.full_url}",
                exc.code,
                detail,
                elapsed_s=elapsed,
            ) from exc
        except urllib.error.URLError as exc:
            elapsed = time.monotonic() - started
            if _url_error_is_timeout(exc):
                raise SyncTransportError(
                    f"{request.get_method()} {request.full_url} client timeout "
                    f"after {elapsed:.1f}s: {exc.reason}"
                ) from exc
            raise SyncTransportError(
                f"{request.get_method()} {request.full_url} failed after "
                f"{elapsed:.1f}s: {exc.reason}"
            ) from exc
        return _decode(body, request.full_url)

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self._url(path),
            data=json.dumps(dict(payload)).encode("utf-8"),
            headers={"Content-Type": "application/json", **self._auth},
            method="POST",
        )
        return self._send(request, timeout_s=self._push_timeout_s)

    def get(
        self, path: str, params: Mapping[str, str | Sequence[str]]
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            self._url(path, params), headers=dict(self._auth), method="GET"
        )
        return self._send(request)


def _decode(body: bytes, url: str) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SyncTransportError(f"{url} did not return JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SyncTransportError(f"{url} returned {type(parsed).__name__}, want object")
    return parsed


__all__ = [
    "API_PREFIX",
    "DEFAULT_TIMEOUT_S",
    "PUSH_TIMEOUT_S",
    "HttpTransport",
    "HubTransport",
    "SyncTransportError",
    "TransportFailure",
    "classify_transport_failure",
    "is_hub_server_error",
    "is_timeout_transport",
    "is_unreachable",
    "refused",
]
