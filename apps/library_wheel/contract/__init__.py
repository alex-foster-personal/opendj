"""Public surface of ``apps.library_wheel`` (ADR-NEW-module-contracts).

Code outside the package imports only this module (delivery layers may also
import ``apps.library_wheel.http`` for ``build_service``). It holds the axis
catalog, the error the wheel raises when the library cannot satisfy its data
contract, and the ``LibraryWheelService`` Protocol naming exactly the
operations outsiders use. Standard library and pydantic only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

CONTRACT_VERSION = "library_wheel/1"


class AxisInfo(BaseModel):
    """One selectable per-track overlay on the genre tree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    label: str
    enabled: bool
    reason: str | None


AXES: tuple[AxisInfo, ...] = (
    AxisInfo(key="genre", label="Genre", enabled=True, reason=None),
    AxisInfo(
        key="decade",
        label="Decade",
        enabled=False,
        reason="release year is not ingested into state.db or master.plain.db by this "
        "repo yet -- no track carries one (found during LIBUX-06 build; not one "
        "of the two prerequisite gaps the requirement itself named)",
    ),
    AxisInfo(key="play_count", label="Play count", enabled=True, reason=None),
    AxisInfo(key="popularity", label="Popularity within genre", enabled=True, reason=None),
    AxisInfo(
        key="overplayed_ness",
        label="Overplayed-ness",
        enabled=False,
        reason="needs a popularity-curve calculation that LIBUX-06 marks as an undefined prerequisite",
    ),
    AxisInfo(key="playlist", label="Playlist membership", enabled=True, reason=None),
    AxisInfo(
        key="set_played_in",
        label="Set played in",
        enabled=False,
        reason="needs SET-08 set-history data and the Open DJ deck observer, neither built yet (LIBUX-06 prerequisite)",
    ),
)


class LibraryWheelError(RuntimeError):
    """The configured library cannot satisfy the wheel's data contract."""


@runtime_checkable
class LibraryWheelService(Protocol):
    """The operations code outside ``apps.library_wheel`` uses today."""

    def query(self, state_db: Path, master_db: Path, *, axis: str = "play_count") -> dict[str, Any]:
        """The JSON-ready genre-wheel read model; raises LibraryWheelError or ValueError."""
        ...

    def genre_tags_by_stable_id(self, state_db: Path, master_db: Path) -> dict[str, str | None]:
        """Every library track's raw genre tag, resolved with the wheel's precedence."""
        ...

    def simple_genre_family(self, tag: str | None) -> tuple[str, str] | None:
        """(family_name, color) for a raw genre tag, or None when no family matches."""
        ...


__all__ = [
    "AXES",
    "CONTRACT_VERSION",
    "AxisInfo",
    "LibraryWheelError",
    "LibraryWheelService",
]
