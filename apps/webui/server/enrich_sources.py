"""Where a usable value comes from, and why the drain is or is not moving (ENRICH-02).

The enrich card used to report only Open DJ's own re-analysis ("BPM and
beatgrid: 16 of 2,270 done") while every present track already had a BPM and
a key from rekordbox, which read as a library missing its data. This module is
the pure policy behind the honest version, fed by the ahead-of-time drain's
coverage snapshot so the card, ``GET /ahead-analysis/coverage``,
``GET /enrich/summary`` and ``enrich_cli summary`` all read one measurement:

* ``usable_counts``: per value lane (BPM, key), how many PRESENT tracks have a
  value at all, split by where it comes from. A library source (rekordbox,
  inferred tags, Mixed In Key) is counted first because it is what the app
  serves by default (``apps.analysis.selection.DEFAULT_SOURCE``); Open DJ is
  credited only for tracks no library source covers.
* ``lane_drain_states``: why each own_* lane is or is not moving right now:
  running, paused while a deck plays, waiting for an earlier lane (the drain
  finishes a lane for EVERY track before starting the next), stalled, or done.

Every count's denominator is ``present`` (tracks whose audio is on this Mac),
per the house rule in ``docs/library-availability.md``.

Requirements (mini-PRD):
  ✔︎ a value from any source counts as ready
    [if] a present track has a rekordbox BPM and no Open DJ record [then] it is ready, from rekordbox
    [if] a track has only an Open DJ record [then] it is ready, from open_dj
    [if] a track has neither [then] it is counted in none
  ✔︎ the drain state is named per lane
    [if] a deck is playing and the lane has work [then] paused_playing
    [if] an earlier lane still has work [then] waiting, naming that lane
    [if] the lane has no work left [then] done, whatever the drain is doing
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: Value lanes and the track field a library source fills for each.
VALUE_FIELDS: dict[str, str] = {"beatgrid": "bpm", "key": "key"}
#: The source name Open DJ's own analysis is credited under.
OPEN_DJ: str = "open_dj"
#: Phases the drain runs before any own_* lane (``ahead_analysis._tick_phases``).
PRE_LANE_PHASES: tuple[str, ...] = ("tags", "strip")
DRAIN_STATES: tuple[str, ...] = ("running", "paused_playing", "waiting", "stalled", "starting", "done", "unavailable")


def usable_counts(
    present: Sequence[str], library_sources: Mapping[str, str], own_done: set[str]
) -> dict[str, Any]:
    """``library_sources`` maps stable_id to the source of a real value
    (BPM above zero, a non-empty key); ``own_done`` is Open DJ's produced,
    non-declined ids."""
    by_source: dict[str, int] = {}
    ready = 0
    for sid in dict.fromkeys(present):
        source = library_sources.get(sid) or (OPEN_DJ if sid in own_done else None)
        if source is None:
            continue
        ready += 1
        by_source[source] = by_source.get(source, 0) + 1
    total = len(set(present))
    ranked = dict(sorted(by_source.items(), key=lambda item: (-item[1], item[0])))
    return {"denominator": "present", "total": total, "ready": ready, "none": total - ready, "by_source": ranked}


def _lane_state(state: str, counts: Mapping[str, Any], ahead: str | None) -> dict[str, str | None]:
    if counts.get("unavailable"):
        return {"state": "unavailable", "waiting_on": None, "reason": str(counts["unavailable"])}
    if not counts.get("missing"):
        return {"state": "done", "waiting_on": None, "reason": None}
    if state == "paused_playing":
        return {"state": "paused_playing", "waiting_on": None, "reason": None}
    if state == "blocked" or state.startswith(("timeout:", "waiting:")):
        return {"state": "stalled", "waiting_on": None, "reason": state}
    if state == "idle":
        return {"state": "starting", "waiting_on": None, "reason": None}
    if ahead is not None:
        return {"state": "waiting", "waiting_on": ahead, "reason": None}
    return {"state": "running", "waiting_on": None, "reason": None}


def lane_drain_states(
    state: str, lanes: Mapping[str, Mapping[str, Any]], order: Sequence[str]
) -> dict[str, dict[str, str | None]]:
    """Per own_* lane in drain ``order``: what the drain is doing about it now."""
    phase = state.removeprefix("ran:")
    ahead: str | None = phase if state.startswith("ran:") and phase in PRE_LANE_PHASES else None
    out: dict[str, dict[str, str | None]] = {}
    for lane in order:
        counts = lanes.get(lane)
        if counts is None:
            continue
        out[lane] = _lane_state(state, counts, ahead)
        if ahead is None and counts.get("missing") and not counts.get("unavailable"):
            ahead = lane
    return out


__all__ = ["DRAIN_STATES", "OPEN_DJ", "PRE_LANE_PHASES", "VALUE_FIELDS", "lane_drain_states", "usable_counts"]
