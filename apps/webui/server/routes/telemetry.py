"""App-usage telemetry endpoints -- the agent-native "is the app open" surface.

There is no UI for this and none is wanted: the whole point is that an agent
coordinating an engine restart can ask the engine instead of asking a human.

    GET /api/v1/telemetry/clients | jq .summary
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..usage_telemetry import Surface, UsageStore

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


class HeartbeatIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    #: Unknown surfaces are refused with a 422 rather than coerced, so a
    #: renamed client shows up as a loud validation error, not a silent row.
    surface: Surface
    page_visible: bool
    app_version: str = Field(min_length=1, max_length=64)


class ClientOut(BaseModel):
    client_id: str
    surface: Surface
    page_visible: bool
    app_version: str
    last_seen_at: str = Field(description="Server wall-clock time of the last heartbeat.")
    seconds_since_seen: float = Field(description="Server monotonic seconds since it checked in.")
    is_open: bool = Field(description="Checked in within the in-use window.")
    in_use: bool = Field(description="Open AND the page is visible on screen.")


class SurfaceActivityOut(BaseModel):
    last_request_at: str | None
    seconds_since_request: float | None


class PassiveActivityOut(BaseModel):
    """Backstop signal: ordinary traffic classified by User-Agent.

    Telemetry and health paths are excluded, so an agent polling this
    endpoint cannot manufacture the activity it is asking about.
    """

    last_request_at: str | None
    seconds_since_last_request: float | None
    by_surface: dict[str, SurfaceActivityOut]


class UsageSummaryOut(BaseModel):
    any_client_open: bool
    any_client_in_use: bool
    desktop_shell_open: bool


class UsageClientsOut(BaseModel):
    clients: list[ClientOut]
    summary: UsageSummaryOut
    passive_activity: PassiveActivityOut
    in_use_window_seconds: float


def _store(request: Request) -> UsageStore:
    return request.app.state.usage_store


@router.post("/heartbeat", response_model=ClientOut)
def record_heartbeat(payload: HeartbeatIn, request: Request) -> ClientOut:
    """Upsert one client's liveness. Server-stamped; the client sends no time."""
    store = _store(request)
    store.record_heartbeat(
        client_id=payload.client_id,
        surface=payload.surface,
        page_visible=payload.page_visible,
        app_version=payload.app_version,
    )
    snapshot = store.snapshot()
    view = next(
        row for row in snapshot["clients"] if row["client_id"] == payload.client_id
    )
    return ClientOut(**view)


@router.get("/clients", response_model=UsageClientsOut)
def list_clients(request: Request) -> UsageClientsOut:
    """Every client seen since engine boot, plus the one-glance summary."""
    return UsageClientsOut(**_store(request).snapshot())
