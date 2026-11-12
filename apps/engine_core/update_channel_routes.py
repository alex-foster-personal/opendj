"""The FastAPI update check and apply routes.

Part of the update channel split out of one oversized module (#9688). Import
from ``apps.engine_core.update_channel``, the public facade, not from here.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from apps.engine_core.build_info import (
    BUILD_IDENTITY_STATE_ATTR,
    BuildIdentity,
)
from apps.engine_core.update_channel_contract import (
    ANSWERED,
    CODE_UPDATE_CHECK_FAILED,
    UPDATE_APPLY_PATH,
    UPDATE_APPLY_STATUS_PATH,
    UPDATE_CHECK_PATH,
    UpdateApplyAccepted,
    UpdateApplyRefused,
    UpdateApplyStatusOut,
    UpdateCheckOut,
)
from apps.engine_core.update_channel_feed import (
    resolve_update_check,
)
from apps.webui.server.shell_commands import (
    ShellBrokerInvalidError,
    ShellBrokerUnavailableError,
    ShellCommandConflictError,
    shell_broker,
)


# ----- route --------------------------------------------------------------
def _update_check_http_response(identity: BuildIdentity, result: UpdateCheckOut) -> UpdateCheckOut | JSONResponse:
    """Map a resolver verdict to the HTTP contract this build source carries.

    Installed payloads answer 502 on a channel fault so a caller that only
    reads the status code cannot treat an outage as reassurance. Developer
    checkouts answer 200 with the named fault in the body: the UI and agents
    branch on ``status``, and e2e surfaces treat any 502 from this route as a
    defect even when the channel is genuinely unpublished.
    """
    if result.status in ANSWERED:
        return result
    info = identity.info
    if info is not None and info.source == "repo":
        return result
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={
            "error": CODE_UPDATE_CHECK_FAILED,
            "message": result.detail,
            **result.model_dump(),
        },
    )


def add_update_check_route(app: FastAPI, *, endpoint: str | None = None) -> None:
    """Mount the check. Resolved per request: the channel moves, the build does not."""

    @app.get(
        UPDATE_CHECK_PATH,
        response_model=UpdateCheckOut,
        tags=["health"],
        name="update_check",
        responses={status.HTTP_502_BAD_GATEWAY: {"description": "the update channel could not be read"}},
    )
    def update_check() -> UpdateCheckOut | JSONResponse:
        identity: BuildIdentity = getattr(app.state, BUILD_IDENTITY_STATE_ATTR)
        with httpx.Client() as client:
            result = resolve_update_check(identity, client=client, endpoint=endpoint)
        return _update_check_http_response(identity, result)


def add_update_apply_route(app: FastAPI, *, endpoint: str | None = None) -> None:
    """Mount apply: enqueue shell work when the channel reports update-available."""

    @app.post(
        UPDATE_APPLY_PATH,
        response_model=UpdateApplyAccepted,
        tags=["health"],
        name="update_apply",
        status_code=status.HTTP_202_ACCEPTED,
        responses={
            status.HTTP_409_CONFLICT: {
                "description": "apply refused; no shell command enqueued",
                "model": UpdateApplyRefused,
            }
        },
    )
    def update_apply(request: Request) -> UpdateApplyAccepted | JSONResponse:
        identity: BuildIdentity = getattr(app.state, BUILD_IDENTITY_STATE_ATTR)
        info = identity.info
        if info is None or info.source != "payload":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=UpdateApplyRefused(
                    status="identity-unavailable",
                    detail="not a packaged build; only installed payloads may apply updates",
                ).model_dump(),
            )
        with httpx.Client() as client:
            result = resolve_update_check(identity, client=client, endpoint=endpoint)
        if result.status != "update-available":
            detail = result.detail or f"update check reported {result.status}"
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=UpdateApplyRefused(
                    status=result.status,
                    detail=detail,
                ).model_dump(),
            )
        available_version = result.available_version
        if available_version is None or available_version.strip() == "":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=UpdateApplyRefused(
                    status=result.status,
                    detail="update-available without available_version",
                ).model_dump(),
            )
        try:
            broker = shell_broker(request)
        except ShellBrokerUnavailableError as error:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=UpdateApplyRefused(status=error.status, detail=error.detail).model_dump(),
            )
        except ShellBrokerInvalidError as error:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=UpdateApplyRefused(status=error.status, detail=error.detail).model_dump(),
            )
        try:
            command_id = broker.enqueue_apply(available_version)
        except ShellCommandConflictError as error:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=UpdateApplyRefused(
                    status="update-available",
                    detail=f"apply-update already pending or claimed (command_id={error.command_id})",
                ).model_dump(),
            )
        return UpdateApplyAccepted(
            available_version=available_version,
            command_id=command_id,
        )

    @app.get(
        UPDATE_APPLY_STATUS_PATH,
        response_model=UpdateApplyStatusOut,
        tags=["health"],
        name="update_apply_status",
    )
    def update_apply_status(request: Request, command_id: str) -> UpdateApplyStatusOut | JSONResponse:
        try:
            broker = shell_broker(request)
        except ShellBrokerUnavailableError as error:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=UpdateApplyRefused(status=error.status, detail=error.detail).model_dump(),
            )
        except ShellBrokerInvalidError as error:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=UpdateApplyRefused(status=error.status, detail=error.detail).model_dump(),
            )
        status_payload = broker.get_status(command_id)
        if status_payload is None:
            raise HTTPException(status_code=404, detail="unknown apply command")
        return UpdateApplyStatusOut(**status_payload)
