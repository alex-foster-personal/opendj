"""HTTP read/write of the per-machine CloudSync config (``cloudsync-config.json``).

``GET /api/v1/cloudsync/config`` and ``PUT /api/v1/cloudsync/config``. The
CLI twin is ``python -m apps.sync_hub config show|set``; both print the same
payload from :func:`apps.sync_hub.config.config_payload`.

PUT is a full replace of the file. The response carries the EFFECTIVE config
too, with the winning source per field, because an env override
(``MDT_CLOUDSYNC_SCHEDULER`` / ``MDT_CLOUDSYNC_HUB_URL``) still wins over
what was just saved and the caller must be able to see that. The running
scheduler re-reads the file on its next wake; no restart is needed.

Local operator only, both verbs, with the same guard as the operator routes
in ``cloudsync_ops``. PUT repoints this machine's scheduler: a tailnet peer
(or a DNS-rebound page) that could write ``hub_url`` would have the next
scheduler round push the library to a hub it controls. GET discloses the hub
URL. The CLI twin needs a shell on the machine, so the HTTP twin needs the
local operator.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from apps.sync_hub import config as sync_config

from .cloudsync_ops import LOCAL_ONLY_RESPONSE, require_local_operator
from .cloudsync_status import UNREADABLE_RESPONSES, ConfigSourceOut, cloudsync_data_dir

router = APIRouter(prefix="/cloudsync", tags=["cloudsync"])

CONFIG_RESPONSES: dict[int | str, dict[str, Any]] = {
    **LOCAL_ONLY_RESPONSE,
    **UNREADABLE_RESPONSES,
}


class CloudSyncEffectiveConfigOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool
    hub_url: str | None
    machine_name: str | None
    configured: bool
    enabled_source: ConfigSourceOut
    hub_url_source: ConfigSourceOut


class CloudSyncConfigOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    file: sync_config.CloudSyncConfig | None
    effective: CloudSyncEffectiveConfigOut


def _payload(request: Request) -> CloudSyncConfigOut:
    try:
        return CloudSyncConfigOut.model_validate(
            sync_config.config_payload(cloudsync_data_dir(request))
        )
    except sync_config.CloudSyncConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "CLOUDSYNC_CONFIG_INVALID",
                "message": str(exc),
            },
        ) from exc


@router.get(
    "/config",
    response_model=CloudSyncConfigOut,
    responses=CONFIG_RESPONSES,
    dependencies=[Depends(require_local_operator)],
)
def get_config(request: Request) -> CloudSyncConfigOut:
    return _payload(request)


@router.put(
    "/config",
    response_model=CloudSyncConfigOut,
    responses=CONFIG_RESPONSES,
    dependencies=[Depends(require_local_operator)],
)
def put_config(body: sync_config.CloudSyncConfig, request: Request) -> CloudSyncConfigOut:
    try:
        sync_config.write_config(cloudsync_data_dir(request), body)
    except sync_config.CloudSyncConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "CLOUDSYNC_CONFIG_UNWRITABLE",
                "message": str(exc),
            },
        ) from exc
    return _payload(request)


__all__ = ["CloudSyncConfigOut", "router"]
