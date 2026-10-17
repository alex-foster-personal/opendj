"""Forward billing and hub readouts from a spoke to its configured CloudSync hub.

The browser and account panel talk to the local engine only; this module is
what adds the spoke's ``sync-credential`` bearer and posts to the hub's
``/api/v1/sync/billing/*`` and ``GET /api/v1/sync/status`` endpoints. Card
data and Polar tokens never touch the spoke daemon.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared.state import machine_identity
from apps.sync_hub import config as sync_config
from apps.sync_hub import spoke_credential
from apps.sync_hub.service_billing import NOT_CONFIGURED_CODE
from apps.sync_hub.transport import API_PREFIX, DEFAULT_TIMEOUT_S

HOSTED_HUB_FEATURE_ID: str = "cloudsync.hosted_hub"
PROTOCOL_CODE: str = "SYNC_HUB_PROTOCOL"


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a hub redirect: urllib would carry the bearer to the new origin."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects)


class SpokeBillingError(RuntimeError):
    """The spoke cannot reach billing (misconfiguration or bad credential file)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 409,
        code: str = NOT_CONFIGURED_CODE,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class HubJsonResponse:
    status_code: int
    body: dict[str, Any]


def _resolve_hub(data_dir: Path) -> tuple[str, str, str | None]:
    effective = sync_config.resolve_config(data_dir)
    hub_url = effective.hub_url
    if hub_url is None or not str(hub_url).strip():
        raise SpokeBillingError(
            "no CloudSync hub is configured; billing is only sold on a hosted Polar hub.",
            code=NOT_CONFIGURED_CODE,
        )
    machine_id = machine_identity.get_or_create_machine_id(data_dir)
    try:
        bearer = spoke_credential.read_credential(data_dir)
    except spoke_credential.SpokeCredentialError as exc:
        raise SpokeBillingError(str(exc), status_code=503, code="SYNC_CREDENTIAL") from exc
    return str(hub_url).rstrip("/"), machine_id, bearer


def _hub_json(
    data_dir: Path,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> HubJsonResponse:
    hub_url, machine_id, bearer = _resolve_hub(data_dir)
    url = f"{hub_url}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    headers: dict[str, str] = {}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    payload_bytes: bytes | None = None
    if json_body is not None:
        # Always this spoke's own id: the credential below is this machine's, so a
        # caller-supplied machine_id would bill or refuse a different machine.
        body = {**json_body, "machine_id": machine_id}
        payload_bytes = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload_bytes, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout_s) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise SpokeBillingError(
                f"the CloudSync hub at {hub_url} redirected (HTTP {exc.code}); "
                "not followed, so the sync credential is never sent elsewhere",
                status_code=502,
                code=PROTOCOL_CODE,
            ) from exc
        raw = exc.read()
        status = exc.code
    except urllib.error.URLError as exc:
        raise SpokeBillingError(
            f"could not reach the CloudSync hub at {hub_url}: {exc.reason}",
            status_code=503,
            code="SYNC_HUB_UNREACHABLE",
        ) from exc
    except OSError as exc:
        # A read timeout or reset after connect is not a URLError.
        raise SpokeBillingError(
            f"the CloudSync hub at {hub_url} did not answer: {exc}",
            status_code=503,
            code="SYNC_HUB_UNREACHABLE",
        ) from exc
    else:
        status = response.status
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    if not isinstance(parsed, dict):
        # Never manufacture a body: a hub that answers non-JSON is a protocol fault.
        raise SpokeBillingError(
            f"the CloudSync hub at {hub_url} answered HTTP {status} without a JSON object body",
            status_code=502,
            code=PROTOCOL_CODE,
        )
    return HubJsonResponse(status, parsed)


def post_billing(data_dir: Path, endpoint: str, payload: dict[str, Any]) -> HubJsonResponse:
    """POST ``/api/v1/sync/billing/{endpoint}`` on the configured hub."""
    return _hub_json(
        data_dir,
        "POST",
        f"{API_PREFIX}/billing/{endpoint}",
        json_body=payload,
    )


def hub_sync_status(data_dir: Path) -> HubJsonResponse:
    """``GET /api/v1/sync/status`` on the configured hub for this machine."""
    _, machine_id, _ = _resolve_hub(data_dir)
    return _hub_json(
        data_dir,
        "GET",
        f"{API_PREFIX}/status",
        query={"machine_id": machine_id},
    )


__all__ = [
    "HOSTED_HUB_FEATURE_ID",
    "PROTOCOL_CODE",
    "HubJsonResponse",
    "SpokeBillingError",
    "hub_sync_status",
    "post_billing",
]
