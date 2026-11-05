"""The ``library_tabs`` UI pref: the library browser's open tabs (LIBUX tabs).

Stored in ``ui-prefs.json`` beside the other prefs. The browser PUTs the whole
value on every tab change; agents drive single actions through
``POST /api/v1/ui-prefs/library-tabs`` (list / open / switch / close), the
HTTP twin of the tab strip. Kept here so the ui_prefs route only wires it.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, model_validator

#: Mirrors MAX_PANE_SLOTS in apps/webui/frontend/src/lib/components/rb/browser/pane-tabs.ts.
MAX_LIBRARY_TABS = 4
DEFAULT_LIBRARY_TABS: dict[str, Any] = {"tabs": [], "active": 0}

LibraryTabsAction = Literal["list", "open", "switch", "close"]


class LibraryTabOut(BaseModel):
    """One tab: its list (null = blank list), per-tab search and sort."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    playlist_id: str | None
    name: str
    search: str = ""
    sort_key: str | None = None
    sort_dir: Literal[1, -1] = 1


class LibraryTabsOut(BaseModel):
    """Open tabs in strip order and the active index. No tabs = nothing saved yet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tabs: list[LibraryTabOut] = []
    active: int = 0


class LibraryTabsActionIn(BaseModel):
    """``open`` needs ``playlist_id`` (``name`` optional); ``switch``/``close`` need ``index``."""

    model_config = ConfigDict(extra="forbid")

    action: LibraryTabsAction
    playlist_id: str | None = None
    name: str | None = None
    index: int | None = None

    @model_validator(mode="after")
    def _fields_match_action(self) -> LibraryTabsActionIn:
        if self.action == "open" and (self.playlist_id is None or self.playlist_id.strip() == ""):
            raise ValueError("action 'open' requires a non-empty 'playlist_id'")
        if self.action in ("switch", "close") and self.index is None:
            raise ValueError(f"action {self.action!r} requires 'index'")
        return self


class LibraryTabsActionOut(BaseModel):
    library_tabs: LibraryTabsOut


def _invalid(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "UI_PREFS_INVALID", "message": message})


def _refuse(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def parse_library_tabs(raw: Any) -> dict[str, Any]:
    """Validate a stored or PUT ``library_tabs`` value. Absent = no saved tabs."""
    if raw is None:
        return {"tabs": [], "active": 0}
    try:
        value = LibraryTabsOut.model_validate(raw)
    except ValueError as exc:
        raise _invalid(f"library_tabs is malformed: {exc}") from exc
    if len(value.tabs) > MAX_LIBRARY_TABS:
        raise _invalid(f"library_tabs holds at most {MAX_LIBRARY_TABS} tabs")
    if value.tabs and not 0 <= value.active < len(value.tabs):
        raise _invalid("library_tabs.active must index an open tab")
    if not value.tabs and value.active != 0:
        raise _invalid("library_tabs.active must be 0 when no tabs are open")
    return value.model_dump()


def merge_library_tabs(current: dict[str, Any], patch: LibraryTabsOut | None) -> None:
    """A PUT replaces the whole tab set (the browser always sends all of it)."""
    if patch is not None:
        current["library_tabs"] = parse_library_tabs(patch.model_dump())


def _require_index(state: dict[str, Any], index: int) -> None:
    if not 0 <= index < len(state["tabs"]):
        raise _invalid(f"no library tab at index {index}; {len(state['tabs'])} open")


def apply_library_tabs_action(current: dict[str, Any], body: LibraryTabsActionIn) -> dict[str, Any]:
    """One tab-strip action on a parsed value; returns the new value."""
    tabs = list(current["tabs"])
    if body.action == "list":
        return current
    elif body.action == "open":
        assert body.playlist_id is not None
        existing = next((i for i, t in enumerate(tabs) if t["playlist_id"] == body.playlist_id), None)
        if existing is not None:
            return {"tabs": tabs, "active": existing}
        if len(tabs) >= MAX_LIBRARY_TABS:
            raise _refuse("LIBRARY_TABS_FULL", f"all {MAX_LIBRARY_TABS} library tabs are open; close one first")
        tab = LibraryTabOut(playlist_id=body.playlist_id, name=body.name or body.playlist_id)
        return {"tabs": [*tabs, tab.model_dump()], "active": len(tabs)}
    elif body.action == "switch":
        assert body.index is not None
        _require_index(current, body.index)
        return {"tabs": tabs, "active": body.index}
    elif body.action == "close":
        assert body.index is not None
        _require_index(current, body.index)
        if len(tabs) == 1:
            raise _refuse("LIBRARY_TABS_LAST", "the last library tab cannot close")
        del tabs[body.index]
        active = current["active"]
        if body.index < active or active >= len(tabs):
            active -= 1
        return {"tabs": tabs, "active": max(active, 0)}
    raise AssertionError(f"unhandled library tabs action {body.action!r}")
