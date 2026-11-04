"""AGENT-8304: the one client-errors query the CLI verb and the MCP tool share.

``opendj client_errors`` and the MCP ``client_errors`` tool both call
:func:`fetch_client_errors` against the running engine's
``GET /api/v1/client-errors`` with the same filters, and both return the same
document, so an agent in any session reads browser errors the same way:

    {"count": N, "filters": {...}, "errors": [{timestamp, build_sha, kind, message, event_id}]}

A non-2xx answer or a body that is not a list of records raises
:class:`ClientErrorsRequestFailed`; it is never an empty result.

-Claude
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import httpx

CLIENT_ERRORS_PATH = "/api/v1/client-errors"
DEFAULT_LIMIT = 50
REQUEST_TIMEOUT_S = 30.0
ROW_FIELDS: tuple[str, ...] = ("timestamp", "build_sha", "kind", "message", "event_id")


@dataclass(frozen=True)
class ClientErrorFilters:
    since: str | None = None
    build_sha: str | None = None
    kind: str | None = None
    contains: str | None = None
    limit: int = DEFAULT_LIMIT
    untriaged: bool = False

    def query_params(self) -> dict[str, str]:
        params = {k: str(v) for k, v in asdict(self).items() if v is not None and k != "untriaged"}
        if self.untriaged:
            params["untriaged"] = "true"
        return params


class ClientErrorsRequestFailed(RuntimeError):
    """The engine refused the query, or answered with something that is not records."""

    def __init__(self, message: str, *, status_code: int | None, detail: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


def _row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": record["received_at"],
        "build_sha": record.get("build_sha"),
        "kind": record["kind"],
        "message": record["message"],
        "event_id": record["event_id"],
    }


def fetch_client_errors(base_url: str, filters: ClientErrorFilters) -> dict[str, Any]:
    """GET the filtered client errors and project each to ``ROW_FIELDS``."""
    url = f"{base_url.rstrip('/')}{CLIENT_ERRORS_PATH}"
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        response = client.get(url, params=filters.query_params(), headers={"Accept": "application/json"})
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:500]
    if not response.is_success:
        detail = body.get("detail", body) if isinstance(body, dict) else body
        raise ClientErrorsRequestFailed(
            f"GET {CLIENT_ERRORS_PATH} returned {response.status_code}: {detail}",
            status_code=response.status_code,
            detail=detail,
        )
    if not isinstance(body, list) or not all(isinstance(r, dict) for r in body):
        raise ClientErrorsRequestFailed(
            f"GET {CLIENT_ERRORS_PATH} returned a body that is not a list of records",
            status_code=response.status_code,
            detail=body,
        )
    rows = [_row(record) for record in body]
    return {"count": len(rows), "filters": asdict(filters), "errors": rows}
