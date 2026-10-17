"""Plugin catalog on the hosted hub (CSPAY-06): index only, no store UI.

``GET /api/v1/sync/plugins/index`` returns the catalog named by required env
``MDT_PLUGIN_CATALOG`` (a JSON list of ``{id, name, version, url, sha256}``).
Unset env answers 409 ``SYNC_PLUGINS_NOT_CONFIGURED``. A hosted hub gates
access on ``pluginstore.access``; a self-hosted hub serves without asking a
source.

CLI twin: ``python -m apps.sync_hub plugins index --data-dir DIR``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from apps import entitlements
from apps.shared.state import db as state_db
from apps.sync_hub import entitlement_gate, service_billing, service_credentials
from apps.sync_hub.service_request import _data_dir, _db_path

router = APIRouter(prefix="/plugins", tags=["sync-plugins"])

CATALOG_ENV: str = "MDT_PLUGIN_CATALOG"
NOT_CONFIGURED_CODE: str = "SYNC_PLUGINS_NOT_CONFIGURED"
CATALOG_INVALID_CODE: str = "SYNC_PLUGINS_CATALOG_INVALID"


class PluginEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=2048)
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

    @field_validator("url")
    @classmethod
    def _https_url(cls, value: str) -> str:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError(f"url must be an absolute https URL, got {value!r}")
        return value


class PluginIndexResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    plugins: tuple[PluginEntry, ...]


_CatalogAdapter: TypeAdapter[list[PluginEntry]] = TypeAdapter(list[PluginEntry])


def _responses(endpoint: str) -> dict[int | str, dict[str, object]]:
    return {
        **service_credentials.credential_responses(endpoint),
        401: {
            "description": (
                f"{endpoint} refused: the Authorization bearer is missing, wrong, revoked, "
                f"or not owned on this hub. A hosted hub refuses in either credential mode; "
                f"a self-hosted hub only under ENFORCE. code: SYNC_CREDENTIAL. "
                f"{service_credentials._ENVELOPE}"
            )
        },
        403: {
            "description": (
                f"{endpoint}: the machine has no owner this hub honors, or "
                f"pluginstore.access is not in plan. "
                f"code: {entitlement_gate.UNOWNED_CODE} or "
                f"{entitlements.NOT_IN_PLAN_CODE}."
            )
        },
        503: {
            "description": (
                f"{endpoint} refused: ENFORCE will not activate while any machine is "
                f"unowned or holds no credential, code SYNC_ENFORCE_NOT_ACTIVE; or the "
                f"hosted hub cannot decide entitlement: more than one owner "
                f"({entitlement_gate.MULTI_OWNER_CODE}), no entitlement source "
                f"({entitlement_gate.NO_SOURCE_CODE}), Polar without enforced "
                f"credentials ({entitlement_gate.NOT_ENFORCED_CODE}), or a provider "
                f"error ({entitlement_gate.PROVIDER_CODE}). {service_credentials._ENVELOPE}"
            )
        },
        409: {"description": (f"{endpoint}: ``{CATALOG_ENV}`` is unset, code {NOT_CONFIGURED_CODE}.")},
        502: {"description": (f"{endpoint}: catalog file missing or invalid, code {CATALOG_INVALID_CODE}.")},
    }


@contextmanager
def _hub_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = state_db.open_rw(_db_path(request))
    try:
        yield conn
    finally:
        conn.close()


def _catalog_path() -> Path:
    raw = os.environ.get(CATALOG_ENV, "").strip()
    if not raw:
        raise HTTPException(
            status_code=409,
            detail={
                "code": NOT_CONFIGURED_CODE,
                "message": (f"{CATALOG_ENV} is unset; this hub has no plugin catalog to serve."),
            },
        )
    return Path(raw)


def load_catalog(path: Path) -> tuple[PluginEntry, ...]:
    """Read and validate the catalog file. Raises ``HTTPException`` on failure."""
    if not path.is_file():
        raise HTTPException(
            status_code=502,
            detail={
                "code": CATALOG_INVALID_CODE,
                "message": f"plugin catalog {path} is not a readable file",
            },
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": CATALOG_INVALID_CODE,
                "message": f"plugin catalog {path} is not valid JSON: {exc}",
            },
        ) from exc
    try:
        entries = _CatalogAdapter.validate_python(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": CATALOG_INVALID_CODE,
                "message": f"plugin catalog {path} failed validation: {exc}",
            },
        ) from exc
    return tuple(entries)


def _gate_plugin_access(request: Request, conn: sqlite3.Connection, machine_id: str) -> None:
    """Hosted-hub ``pluginstore.access``; no-op on a self-hosted hub.

    Uses ``write`` so ``read_only`` and ``archived`` are refused: the plugin
    catalog is paid access, not library pull-out during a lapse.
    """
    entitlement_gate.require(
        request,
        conn,
        machine_id=machine_id,
        operation="write",
        data_dir=lambda: _data_dir(request),
        feature_id=entitlement_gate.PLUGINSTORE_ACCESS_FEATURE,
    )


@router.get(
    "/index",
    response_model=PluginIndexResponse,
    responses=_responses("plugins/index"),
)
def get_plugin_index(
    request: Request,
    machine_id: str = Query(min_length=1, description="the calling spoke"),
) -> PluginIndexResponse:
    """Return the configured plugin catalog for an authenticated owner."""
    if entitlement_gate.is_hosted(request):
        # Hosted: the single-owner and owned-machine rules billing applies,
        # plus a valid credential in either mode (the catalog is paid).
        service_billing._owner(request, machine_id, "plugins/index", require_valid=True)
    else:
        # Self-hosted serves without a gate (CSPAY-06): the credential only,
        # never the hosted owner rules, so a shared family hub still works.
        with _hub_conn(request) as conn:
            service_credentials.require_credential(
                request, conn, machine_id, data_dir=_data_dir(request), endpoint="plugins/index"
            )
    with _hub_conn(request) as conn:
        _gate_plugin_access(request, conn, machine_id)
    # Configuration is named only to a caller that passed every gate.
    return PluginIndexResponse(plugins=load_catalog(_catalog_path()))


__all__ = [
    "CATALOG_ENV",
    "CATALOG_INVALID_CODE",
    "NOT_CONFIGURED_CODE",
    "PluginEntry",
    "PluginIndexResponse",
    "load_catalog",
    "router",
]
