"""Public contract of ``apps.play_analytics`` (ADR-NEW-module-contracts, M1).

Code outside the package imports only this module (delivery layers may also
import ``apps.play_analytics.http``). It depends on stdlib and pydantic only.

The read model itself is still returned as a plain JSON-ready ``dict`` so the
HTTP and CLI wire output stays byte-identical; the pydantic models below
describe that dict's shape and are what consumers may validate it against.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

CONTRACT_VERSION = "play_analytics/1"

ShareState = Literal["private", "shared_local", "shared_cloud"]
EventsTable = Literal["events", "set_events"]

#: Default read-time dwell filter, in seconds of audible playback.
#:
#: Applies ONLY to rows that carry an ``audible_s`` value, which today
#: means the Open DJ own-deck source. Rows from ``rb_history`` and
#: ``djay_monitor`` carry no dwell -- their upstream app already decided
#: what earned a history row -- so they are never filtered here.
#:
#: 60s comes from (private path), which
#: measured 3678 real dwell gaps and found NO trough: the populations a
#: threshold would separate are not separated in the data. 60s is where
#: the flat sub-60s shelf gives way to the mix mode, and it sits below
#: p10 (77s). It is a shoulder, not a calibrated constant, which is
#: exactly why it lives here as an adjustable read-time default rather
#: than as a capture-time gate that would destroy the evidence.
DEFAULT_MIN_AUDIBLE_S: float = 60.0


class AnalyticsSchemaError(RuntimeError):
    """The configured event store cannot satisfy the analytics contract."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PlayAnalyticsFilters(_Frozen):
    share_state: ShareState | None
    limit: int
    min_audible_s: float


class PlayAnalyticsSummary(_Frozen):
    sessions: int
    plays: int
    unique_tracks: int
    completed_duration_s: float
    plays_below_min_audible: int


class PlayAnalyticsSession(_Frozen):
    session_id: str
    started_at: str
    ended_at: str | None
    duration_s: float | None
    share_state: str
    play_count: int
    unique_track_count: int


class PlayAnalyticsTopTrack(_Frozen):
    stable_id: str
    title: str | None
    artist: str | None
    play_count: int
    last_played_at: str


class PlayAnalyticsReadModel(_Frozen):
    """Shape of the dict ``PlayAnalyticsService.query_play_analytics`` returns."""

    schema_version: Literal[1]
    filters: PlayAnalyticsFilters
    summary: PlayAnalyticsSummary
    sessions: list[PlayAnalyticsSession]
    top_tracks: list[PlayAnalyticsTopTrack]


@runtime_checkable
class PlayAnalyticsService(Protocol):
    """Operations outsiders may perform on play analytics."""

    def query_play_analytics(
        self,
        db_path: Path,
        *,
        share_state: ShareState | None = None,
        limit: int = 50,
        events_table: EventsTable = "events",
        min_audible_s: float = DEFAULT_MIN_AUDIBLE_S,
    ) -> dict[str, Any]:
        """Return the JSON-ready read model (see ``PlayAnalyticsReadModel``).

        Raises ``AnalyticsSchemaError`` when the event store cannot satisfy
        the contract and ``ValueError`` for out-of-range arguments.
        """
        ...


__all__ = [
    "CONTRACT_VERSION",
    "DEFAULT_MIN_AUDIBLE_S",
    "AnalyticsSchemaError",
    "EventsTable",
    "PlayAnalyticsFilters",
    "PlayAnalyticsReadModel",
    "PlayAnalyticsService",
    "PlayAnalyticsSession",
    "PlayAnalyticsSummary",
    "PlayAnalyticsTopTrack",
    "ShareState",
]
