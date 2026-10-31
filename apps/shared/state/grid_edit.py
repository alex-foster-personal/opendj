"""Open DJ's own beatgrid edit: the deck's grid adjust and grid shift (GRIDEDIT-6235..6235d).

The two left-edge deck buttons (rekordbox's grid-edit controls) never touch
the grid a producer measured. rekordbox's master db is read-only to us, and an
own analysis record is the analyzer's answer, so a user correction is stored
beside both as the provenance-wrapped ``track_fields`` row ``beatgrid_edit``
(source ``webui``) and applied when ``/anlz`` serves the grid. Clearing it is
a write of the identity edit, so ``track_field_history`` keeps every earlier
value and the row travels with CloudSync like every other user field.

The value is one JSON object per track, one edit PER GRID SOURCE::

    {"v": 1,
     "edits": {"rekordbox": {"offset_ms": 6.0, "beat_shift": 1},
               "own":       {"offset_ms": 0.0, "beat_shift": 0}}}

Per source because the two grids are different measurements: a nudge that
lines rekordbox's grid up with the kick says nothing about where the own
analyzer put its beats, so switching the PARITY-02 lane must not carry it over.

Two operations, matching the two buttons:

* ``offset_ms`` (grid adjust) moves every beat by the same time, later when
  positive. Tempo is untouched: each beat keeps its ``bpm``. A beat moved
  before 0 s is dropped, since no consumer can schedule a negative time.
* ``beat_shift`` (grid shift) moves bar 1 by whole beats, later when
  positive. The beat TIMES stay where they are (a whole-beat move of a grid
  lands every line on another line), so only the bar position ``n`` is
  relabelled. That is exact for a variable-tempo grid too, where moving the
  times by "one beat" would have no single length. Stored modulo the bar,
  normalized to ``-1..2``.

This module owns no connection and opens no transaction; the routes hold
``BEGIN IMMEDIATE`` around a read-modify-write, as the hot-cue routes do.

-Claude
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

FIELD: Final = "beatgrid_edit"
BLOB_VERSION: Final = 1
#: ``track_fields.source`` of an edit made in Open DJ itself.
OWN_SOURCE: Final = "webui"
#: Beats per bar of every grid ``/anlz`` serves (PQTZ and own grids number 1..4).
BEATS_PER_BAR: Final = 4
#: Largest stored grid-adjust offset, either way. Half a second is a whole beat
#: below 120 BPM; past a beat the right tool is grid shift, not a nudge.
MAX_OFFSET_MS: Final = 500.0
#: Offsets are kept to a microsecond so repeated float steps cannot drift
#: (0.1 + 0.2 style) into a value no click produced.
OFFSET_DECIMALS: Final = 3

GridSource = Literal["rekordbox", "own"]
GRID_SOURCES: Final[tuple[GridSource, ...]] = ("rekordbox", "own")


class GridEditError(ValueError):
    """A refused edit, carrying the HTTP status and code it maps to."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class GridEdit:
    """One grid source's edit. The default is the identity (no edit)."""

    offset_ms: float = 0.0
    beat_shift: int = 0

    def is_identity(self) -> bool:
        return self.offset_ms == 0.0 and self.beat_shift == 0

    def as_dict(self) -> dict[str, Any]:
        return {"offset_ms": self.offset_ms, "beat_shift": self.beat_shift}


IDENTITY: Final = GridEdit()


# ---------- the math ---------------------------------------------------------


def normalize_beat_shift(shift: int) -> int:
    """``shift`` modulo the bar, in ``-1..2``: four beats is the same bar 1."""
    return ((shift + 1) % BEATS_PER_BAR) - 1


def _checked_offset(offset_ms: float) -> float:
    if not math.isfinite(offset_ms):
        raise GridEditError(422, "GRID_EDIT_INVALID", f"offset_ms must be finite, got {offset_ms!r}")
    rounded = round(offset_ms, OFFSET_DECIMALS)
    if abs(rounded) > MAX_OFFSET_MS:
        raise GridEditError(
            422,
            "GRID_EDIT_OUT_OF_RANGE",
            f"grid offset {rounded:g} ms is past the {MAX_OFFSET_MS:g} ms limit; move bar 1 with grid shift instead",
        )
    # -0.0 would serialize as "-0.0" and make two identity edits differ.
    return rounded + 0.0


def make_edit(offset_ms: float, beat_shift: int) -> GridEdit:
    """A validated, normalized edit (absolute values)."""
    return GridEdit(offset_ms=_checked_offset(offset_ms), beat_shift=normalize_beat_shift(beat_shift))


def nudged(edit: GridEdit, *, offset_ms: float = 0.0, beats: int = 0) -> GridEdit:
    """``edit`` moved by a relative step: the grid-adjust and grid-shift clicks.

    One request can carry several held presses merged by the client, so a step
    that would pass the offset limit lands AT the limit, keeping the presses
    that fit. A step from an offset already at the limit outward moves nothing
    and is refused, so the deck still says it hit the limit.
    """
    if not math.isfinite(offset_ms):
        raise GridEditError(422, "GRID_EDIT_INVALID", f"offset_ms must be finite, got {offset_ms!r}")
    target = edit.offset_ms + offset_ms
    if abs(target) > MAX_OFFSET_MS and abs(edit.offset_ms) < MAX_OFFSET_MS:
        target = math.copysign(MAX_OFFSET_MS, target)
    return make_edit(target, edit.beat_shift + beats)


def relabel_beat_number(n: int, beat_shift: int) -> int:
    """Bar position of a beat once bar 1 has moved ``beat_shift`` beats later.

    Moving bar 1 one beat LATER means the beat that was 2 becomes 1 and the
    old downbeat becomes 4 (it now ends the previous bar).
    """
    if not 1 <= n <= BEATS_PER_BAR:
        raise ValueError(f"beat number {n!r} is outside 1..{BEATS_PER_BAR}; cannot shift bar 1")
    return ((n - 1 - beat_shift) % BEATS_PER_BAR) + 1


def apply_to_beats(beats: Sequence[Mapping[str, Any]], edit: GridEdit) -> list[dict[str, Any]]:
    """The beats with ``edit`` applied. New dicts; the input is not mutated."""
    if edit.is_identity():
        return [dict(beat) for beat in beats]
    offset_s = edit.offset_ms / 1000.0
    out: list[dict[str, Any]] = []
    for beat in beats:
        moved = dict(beat)
        # A shift-only edit relabels beat numbers and moves no time, so the
        # stored times pass through untouched rather than re-rounded.
        if offset_s != 0.0:
            t = round(float(beat["t"]) + offset_s, 6)
            if t < 0:
                continue
            moved["t"] = t
        if edit.beat_shift != 0:
            moved["n"] = relabel_beat_number(int(beat["n"]), edit.beat_shift)
        out.append(moved)
    return out


def apply_to_payload(payload: dict[str, Any], edit: GridEdit, source: GridSource) -> dict[str, Any]:
    """Apply ``edit`` to an ``/anlz`` payload in place and stamp ``beatgrid.edit``.

    The stamp lets a client read the edit in force from the same payload as
    the grid it applies to, never from a second read. It rides INSIDE the
    beatgrid block because every client path that adopts a new grid onto a
    loaded deck replaces that block whole, so the edit can never be left
    describing a grid the deck no longer holds. The identity is NOT stamped:
    an unedited track's payload (and so its ETag) is byte-identical to what it
    was before edits existed, and an absent ``edit`` reads as no edit.
    ``tempo_changes`` markers move with the beats they describe.
    """
    if edit.is_identity():
        return payload
    grid = dict(payload.get("beatgrid") or {})
    beats = grid.get("beats") or []
    if beats:
        moved = apply_to_beats(beats, edit)
        grid["beats"] = moved
        grid["beat_count"] = len(moved)
    # Markers move with the offset whether or not this payload carries beats,
    # so a stamped edit never leaves them at their measured times (Sol P1, #6235).
    changes = payload.get("tempo_changes")
    if changes and edit.offset_ms != 0.0:
        offset_s = edit.offset_ms / 1000.0
        payload["tempo_changes"] = [
            {**change, "at_s": round(float(change["at_s"]) + offset_s, 6)} for change in changes
        ]
    grid["edit"] = {"source": source, **edit.as_dict()}
    payload["beatgrid"] = grid
    return payload


# ---------- the store ----------------------------------------------------------


@dataclass(frozen=True)
class StoredGridEdits:
    """Every source's edit for one track, plus the row's provenance."""

    edits: dict[str, GridEdit]
    source: str | None
    modified_at: str | None

    def for_source(self, source: GridSource) -> GridEdit:
        return self.edits.get(source, IDENTITY)


NO_EDITS: Final = StoredGridEdits(edits={}, source=None, modified_at=None)


def _parse_blob(stable_id: str, raw: str) -> dict[str, GridEdit]:
    """The stored edits, validated strictly: a malformed blob is corruption and
    raises, never reads as "no edit" or a coerced value (Sol P1, PR #6235)."""
    value = json.loads(raw)
    # `type(...) is int`: 1.0 and true compare equal to 1 (Sol P1, PR #6235).
    if not isinstance(value, dict) or type(value.get("v")) is not int or value["v"] != BLOB_VERSION or set(value) != {"v", "edits"}:
        raise ValueError(f"{FIELD} for {stable_id} is not a v{BLOB_VERSION} edit: {raw[:80]!r}")
    stored = value["edits"]
    if not isinstance(stored, dict):
        raise ValueError(f"{FIELD} for {stable_id} has no edits object: {raw[:80]!r}")
    edits: dict[str, GridEdit] = {}
    for source, edit in stored.items():
        if source not in GRID_SOURCES:
            raise ValueError(f"{FIELD} for {stable_id} names unknown grid source {source!r}")
        edits[source] = _parse_edit(f"{FIELD} for {stable_id} {source}", edit)
    return edits


def _parse_edit(where: str, edit: object) -> GridEdit:
    """One source's stored edit, with no key or type coerced."""
    if not isinstance(edit, dict) or set(edit) != {"offset_ms", "beat_shift"}:
        raise ValueError(f"{where} edit is malformed: {edit!r}")
    offset_ms, beat_shift = edit["offset_ms"], edit["beat_shift"]
    if isinstance(offset_ms, bool) or not isinstance(offset_ms, (int, float)) or not math.isfinite(offset_ms):
        raise ValueError(f"{where} offset_ms is not a finite number: {offset_ms!r}")
    if isinstance(beat_shift, bool) or not isinstance(beat_shift, int):
        raise ValueError(f"{where} beat_shift is not an integer: {beat_shift!r}")
    return make_edit(float(offset_ms), beat_shift)


def read(conn: sqlite3.Connection, stable_id: str) -> StoredGridEdits:
    """The track's stored edits; no row reads as no edit at all."""
    row = conn.execute(
        "SELECT value_json, source, modified_at FROM track_fields "
        "WHERE stable_id = ? AND field_name = ? AND deleted_at IS NULL",
        (stable_id, FIELD),
    ).fetchone()
    if row is None:
        return NO_EDITS
    return StoredGridEdits(edits=_parse_blob(stable_id, str(row[0])), source=str(row[1]), modified_at=str(row[2]))


def blob(edits: Mapping[GridSource, GridEdit]) -> dict[str, Any]:
    """The stored value. Identity edits are kept, so a reset is a real write."""
    return {"v": BLOB_VERSION, "edits": {src: edits[src].as_dict() for src in sorted(edits)}}


def with_edit(stored: StoredGridEdits, source: GridSource, edit: GridEdit) -> dict[GridSource, GridEdit]:
    """Every source's edit after replacing ``source``'s."""
    edits: dict[GridSource, GridEdit] = {src: stored.edits[src] for src in GRID_SOURCES if src in stored.edits}
    edits[source] = edit
    return edits


__all__ = [
    "BEATS_PER_BAR",
    "BLOB_VERSION",
    "FIELD",
    "GRID_SOURCES",
    "IDENTITY",
    "MAX_OFFSET_MS",
    "NO_EDITS",
    "OWN_SOURCE",
    "GridEdit",
    "GridEditError",
    "GridSource",
    "StoredGridEdits",
    "apply_to_beats",
    "apply_to_payload",
    "blob",
    "make_edit",
    "normalize_beat_shift",
    "nudged",
    "read",
    "relabel_beat_number",
    "with_edit",
]
