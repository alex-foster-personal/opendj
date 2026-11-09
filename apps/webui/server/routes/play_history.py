"""Play history routes (logged deck loads).

``DELETE /history/{day}`` removes every Open DJ play from local 06:00 on that
calendar day until 06:00 the next day (``play_log.HISTORY_DAY_ROLLOVER_HOUR``).
The optional ``tz`` query is an IANA zone name; when omitted the server's local
zone is used (the desktop app runs colocated with the browser). Issue #9745
will add ``GET`` list routes in this same module (not ``playlist_history.py``,
which is playlist navigation history).

Mounted from ``app_wiring.py`` under the API prefix.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state import play_log

from ..backend import StateBackend
from ..deps import get_write_state
from .track_plays import _state_db_path

router = APIRouter(prefix="/history", tags=["play-history"])
MOUNT_UNDER_API_PREFIX = True


class HistoryDayDeleteOut(BaseModel):
    date: str = Field(description="YYYY-MM-DD as requested")
    start: str = Field(description="Canonical UTC bound used (inclusive)")
    end: str = Field(description="Canonical UTC bound used (exclusive)")
    removed: int
    stable_ids: list[str]


@router.delete("/{day}", response_model=HistoryDayDeleteOut)
def delete_history_day(
    day: date,
    request: Request,
    tz: str | None = Query(
        default=None,
        description="IANA timezone for the 06:00 day boundary; default is server local.",
    ),
    _backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> HistoryDayDeleteOut:
    zone = None
    if tz is not None:
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise HTTPException(
                status_code=422,
                detail={"code": "BAD_TZ", "message": f"unknown timezone {tz!r}"},
            ) from error
    start, end = play_log.history_day_bounds(day, zone)
    conn = state_db.open_rw(_state_db_path(request))
    try:
        conn.execute("BEGIN IMMEDIATE")
        deleted = play_log.delete_plays_between(conn, start, end)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if deleted.count > 0:
        publish("library.changed", {"kind": "plays", "ids": list(deleted.stable_ids)})
    return HistoryDayDeleteOut(
        date=day.isoformat(),
        start=start,
        end=end,
        removed=deleted.count,
        stable_ids=list(deleted.stable_ids),
    )


__all__ = ["HistoryDayDeleteOut", "router"]
