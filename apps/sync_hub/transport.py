"""The JSON transport a spoke talks to its hub over.

Split out of :mod:`apps.sync_hub.client` so that module is about the round
trip and this one is about moving bytes.

Transport is a two-method protocol rather than a hard httpx dependency, for
the same reason ``apps.cloud.lock`` takes an :class:`~apps.cloud.lock.S3Client`
protocol: the test hub is a Starlette ``TestClient``, the real hub is a URL
over the tailnet, and neither one is a mock of the other. The default
implementation is stdlib ``urllib`` so importing this costs nothing beyond
the standard library.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol

API_PREFIX: str = "/api/v1/sync"
DEFAULT_TIMEOUT_S: float = 30.0


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


def refused(label: str, status_code: int, body: str) -> SyncTransportError:
    """The error for a non-2xx answer. Shared by every :class:`HubTransport`."""
    return SyncTransportError(
        f"{label} -> HTTP {status_code}: {body}",
        status_code=status_code,
        code=_detail_code(body),
    )


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

    def get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        """GET ``path`` with query ``params``; return the decoded JSON body."""


class HttpTransport:
    """stdlib-only JSON transport. Any non-2xx is an error, never a default."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
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
        self._auth: dict[str, str] = (
            {} if bearer is None else {"Authorization": f"Bearer {bearer}"}
        )

    def _url(self, path: str, params: Mapping[str, str] | None = None) -> str:
        url = f"{self._base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(dict(params))}"
        return url

    def _send(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise refused(
                f"{request.get_method()} {request.full_url}", exc.code, detail
            ) from exc
        except urllib.error.URLError as exc:
            raise SyncTransportError(
                f"{request.get_method()} {request.full_url} failed: {exc.reason}"
            ) from exc
        return _decode(body, request.full_url)

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self._url(path),
            data=json.dumps(dict(payload)).encode("utf-8"),
            headers={"Content-Type": "application/json", **self._auth},
            method="POST",
        )
        return self._send(request)

    def get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
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
    "HttpTransport",
    "HubTransport",
    "SyncTransportError",
    "is_hub_server_error",
    "is_unreachable",
    "refused",
]
