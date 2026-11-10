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

A third operation, anchor/BPM retempo (issue #11138), rebuilds every beat time
at a new constant tempo with one grid line fixed; the math lives in
``grid_edit_bpm``. A fourth operation, tempo anchors (GRIDEDIT-10220), is rekordbox's dynamic
grid edit: ``anchors`` is a sorted list of ``{"at_s", "bpm"}``. ``at_s`` is in
SERVED time (after the offset; order is offset, anchors, then the bar 1
relabel). Each anchor snaps to the current grid line nearest ``at_s`` and
regenerates the beats from there at its tempo to the end of the grid's span,
with bar numbering continuous. ``tempo_changes`` is rewritten so its markers
describe the anchors, and the grid is stamped ``static_grid_untrusted``.
The key is written only when anchors exist, so an edit without them
serializes exactly as before::

    {"offset_ms": 0.0, "beat_shift": 0, "anchors": [{"at_s": 60.0, "bpm": 126.0}]}

This module owns no connection and opens no transaction; the routes hold
``BEGIN IMMEDIATE`` around a read-modify-write, as the hot-cue routes do.

Undo (GRIDEDIT-10215) is a read of ``track_field_history``, not new storage.
An undo writes the restored state back WITH that state's own ``modified_at``,
while every real edit stamps a fresh time, so a ``modified_at`` already seen
in history marks a restore. Without that marker a second undo would find the
value the first undo replaced and redo it. CloudSync's LWW key is
``updated_at``, stamped fresh on every write, so this is sync-safe.

-Claude
"""

from __future__ import annotations

import itertools
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
#: Tempo bounds of a user-set BPM (same names and values as the #10112 BPM override).
MIN_BPM: Final = 40.0
MAX_BPM: Final = 300.0
BPM_DECIMALS: Final = 2
#: Most tempo anchors one grid edit holds.
MAX_ANCHORS: Final = 64
ANCHOR_AT_DECIMALS: Final = 3

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
class Anchor:
    """A tempo change placed by hand: from the grid line nearest ``at_s`` on, ``bpm``."""

    at_s: float
    bpm: float

    def as_dict(self) -> dict[str, float]:
        return {"at_s": self.at_s, "bpm": self.bpm}


@dataclass(frozen=True)
class GridEdit:
    """One grid source's edit. The default is the identity (no edit)."""

    offset_ms: float = 0.0
    beat_shift: int = 0
    anchors: tuple[Anchor, ...] = ()
    #: Constant-tempo override; paired with ``anchor_t`` in served time (after offset).
    bpm: float | None = None
    anchor_t: float | None = None

    def is_identity(self) -> bool:
        return self.offset_ms == 0.0 and self.beat_shift == 0 and not self.anchors and self.bpm is None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"offset_ms": self.offset_ms, "beat_shift": self.beat_shift}
        if self.anchors:
            out["anchors"] = [anchor.as_dict() for anchor in self.anchors]
        if self.bpm is not None:
            out["bpm"] = self.bpm
            out["anchor_t"] = self.anchor_t
        return out


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


def _checked_anchor(at_s: float, bpm: float) -> Anchor:
    if not math.isfinite(at_s) or at_s < 0:
        raise GridEditError(422, "GRID_EDIT_INVALID", f"anchor at_s must be a finite time >= 0, got {at_s!r}")
    rounded_bpm = round(bpm, BPM_DECIMALS) if math.isfinite(bpm) else bpm
    if not math.isfinite(rounded_bpm) or not MIN_BPM <= rounded_bpm <= MAX_BPM:
        raise GridEditError(
            422, "GRID_EDIT_OUT_OF_RANGE", f"anchor bpm {bpm!r} is outside {MIN_BPM:g}..{MAX_BPM:g}"
        )
    return Anchor(at_s=round(at_s, ANCHOR_AT_DECIMALS) + 0.0, bpm=rounded_bpm + 0.0)


def _checked_bpm_override(bpm: float | None, anchor_t: float | None) -> tuple[float | None, float | None]:
    """A user BPM override and the served-time anchor it is tied to."""
    if bpm is None and anchor_t is None:
        return None, None
    if bpm is None or anchor_t is None:
        raise GridEditError(422, "GRID_EDIT_INVALID", "bpm and anchor_t must be set together or both omitted")
    if isinstance(bpm, bool) or not isinstance(bpm, (int, float)) or not math.isfinite(bpm):
        raise GridEditError(422, "GRID_EDIT_INVALID", f"bpm must be a finite number, got {bpm!r}")
    rounded_bpm = round(float(bpm), BPM_DECIMALS) + 0.0
    if not MIN_BPM <= rounded_bpm <= MAX_BPM:
        raise GridEditError(
            422, "GRID_EDIT_OUT_OF_RANGE", f"grid BPM {rounded_bpm:g} is outside {MIN_BPM:g}..{MAX_BPM:g}"
        )
    if not math.isfinite(anchor_t):
        raise GridEditError(422, "GRID_EDIT_INVALID", f"anchor_t must be finite, got {anchor_t!r}")
    return rounded_bpm, round(float(anchor_t), ANCHOR_AT_DECIMALS) + 0.0


def _checked_anchors(anchors: Sequence[Anchor]) -> tuple[Anchor, ...]:
    checked = sorted((_checked_anchor(a.at_s, a.bpm) for a in anchors), key=lambda a: a.at_s)
    if len(checked) > MAX_ANCHORS:
        raise GridEditError(422, "GRID_ANCHOR_LIMIT", f"a grid edit holds at most {MAX_ANCHORS} tempo anchors")
    if any(b.at_s <= a.at_s for a, b in itertools.pairwise(checked)):
        raise GridEditError(422, "GRID_EDIT_INVALID", "tempo anchors must have distinct times")
    return tuple(checked)


def make_edit(
    offset_ms: float,
    beat_shift: int,
    *,
    anchors: Sequence[Anchor] = (),
    bpm: float | None = None,
    anchor_t: float | None = None,
) -> GridEdit:
    """A validated, normalized edit (absolute values)."""
    checked_bpm, checked_anchor_t = _checked_bpm_override(bpm, anchor_t)
    return GridEdit(
        offset_ms=_checked_offset(offset_ms),
        beat_shift=normalize_beat_shift(beat_shift),
        anchors=_checked_anchors(anchors),
        bpm=checked_bpm,
        anchor_t=checked_anchor_t,
    )


def _nearest_anchor_index(anchors: Sequence[Anchor], at_s: float) -> int | None:
    """The anchor within half a beat (at its own tempo) of ``at_s``; the nearest wins."""
    near = [(abs(a.at_s - at_s), i) for i, a in enumerate(anchors) if abs(a.at_s - at_s) < 30.0 / a.bpm]
    return min(near)[1] if near else None


def with_anchor(edit: GridEdit, at_s: float, bpm: float) -> GridEdit:
    """``edit`` with a tempo anchor added; one within half a beat of an existing anchor replaces it."""
    new = _checked_anchor(at_s, bpm)
    anchors = list(edit.anchors)
    hit = _nearest_anchor_index(anchors, new.at_s)
    if hit is not None:
        del anchors[hit]
    return make_edit(
        edit.offset_ms, edit.beat_shift, anchors=[*anchors, new], bpm=edit.bpm, anchor_t=edit.anchor_t
    )


def without_anchor(edit: GridEdit, at_s: float) -> GridEdit:
    """``edit`` with the anchor within half a beat of ``at_s`` removed."""
    hit = _nearest_anchor_index(edit.anchors, at_s) if math.isfinite(at_s) else None
    if hit is None:
        raise GridEditError(404, "GRID_ANCHOR_NOT_FOUND", f"no tempo anchor near {at_s:g} s")
    anchors = [a for i, a in enumerate(edit.anchors) if i != hit]
    return make_edit(edit.offset_ms, edit.beat_shift, anchors=anchors, bpm=edit.bpm, anchor_t=edit.anchor_t)


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
    return make_edit(
        target, edit.beat_shift + beats, anchors=edit.anchors, bpm=edit.bpm, anchor_t=edit.anchor_t
    )


def relabel_beat_number(n: int, beat_shift: int) -> int:
    """Bar position of a beat once bar 1 has moved ``beat_shift`` beats later.

    Moving bar 1 one beat LATER means the beat that was 2 becomes 1 and the
    old downbeat becomes 4 (it now ends the previous bar).
    """
    if not 1 <= n <= BEATS_PER_BAR:
        raise ValueError(f"beat number {n!r} is outside 1..{BEATS_PER_BAR}; cannot shift bar 1")
    return ((n - 1 - beat_shift) % BEATS_PER_BAR) + 1


def _offset_beats(beats: Sequence[Mapping[str, Any]], offset_ms: float) -> list[dict[str, Any]]:
    if offset_ms == 0.0:
        # Times pass through untouched rather than re-rounded.
        return [dict(beat) for beat in beats]
    offset_s = offset_ms / 1000.0
    out: list[dict[str, Any]] = []
    for beat in beats:
        t = round(float(beat["t"]) + offset_s, 6)
        if t >= 0:
            out.append({**beat, "t": t})
    return out


def _nearest_beat_index(grid: Sequence[Mapping[str, Any]], at_s: float) -> int:
    """Index of the grid line nearest ``at_s``; a tie goes to the earlier line."""
    return min(range(len(grid)), key=lambda i: (abs(float(grid[i]["t"]) - at_s), i))


def _anchored_grid(
    beats: list[dict[str, Any]], markers: list[dict[str, Any]], anchors: Sequence[Anchor]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Beats regenerated from each anchor at its tempo, and the markers that describe them."""
    grid = beats
    end_t = float(beats[-1]["t"])
    for anchor in anchors:
        i = _nearest_beat_index(grid, anchor.at_s)
        start = grid[i]
        start_t, start_n = float(start["t"]), int(start["n"])
        period = 60.0 / anchor.bpm
        count = math.floor((end_t - start_t) / period + 1e-9) + 1
        regenerated = [
            {
                **start,
                "t": start["t"] if k == 0 else round(start_t + k * period, 6),
                "n": ((start_n - 1 + k) % BEATS_PER_BAR) + 1,
                "bpm": anchor.bpm,
            }
            for k in range(count)
        ]
        markers = [m for m in markers if float(m["at_s"]) < start_t]
        if i > 0:
            markers.append(
                {"at_s": start_t, "bpm_before": grid[i - 1].get("bpm"), "bpm_after": anchor.bpm, "confidence": 1.0}
            )
        grid = grid[:i] + regenerated
    return grid, markers


def _offset_markers(markers: Sequence[Mapping[str, Any]], offset_ms: float) -> list[dict[str, Any]]:
    offset_s = offset_ms / 1000.0
    return [{**m, "at_s": round(float(m["at_s"]) + offset_s, 6)} for m in markers]


from apps.shared.state.grid_edit_bpm import rebuild_beats_at_bpm


def _edited_grid(
    beats: Sequence[Mapping[str, Any]], markers: Sequence[Mapping[str, Any]], edit: GridEdit
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Offset, then BPM override, then tempo anchors, then the bar 1 relabel."""
    moved = _offset_beats(beats, edit.offset_ms)
    moved_markers = _offset_markers(markers, edit.offset_ms) if edit.offset_ms != 0.0 else [dict(m) for m in markers]
    if edit.bpm is not None and edit.anchor_t is not None and moved:
        moved = rebuild_beats_at_bpm(moved, edit.anchor_t, edit.bpm)
        moved_markers = []
    if edit.anchors and moved:
        moved, moved_markers = _anchored_grid(moved, moved_markers, edit.anchors)
    if edit.beat_shift != 0:
        moved = [{**beat, "n": relabel_beat_number(int(beat["n"]), edit.beat_shift)} for beat in moved]
    return moved, moved_markers


def apply_to_beats(beats: Sequence[Mapping[str, Any]], edit: GridEdit) -> list[dict[str, Any]]:
    """The beats with ``edit`` applied. New dicts; the input is not mutated."""
    if edit.is_identity():
        return [dict(beat) for beat in beats]
    return _edited_grid(beats, [], edit)[0]


def _set_dynamic_tempo_hint(payload: dict[str, Any], dynamic: bool) -> None:
    """A NEW hints dict (cached payloads share nested dicts); an empty one is dropped."""
    hints = {k: v for k, v in (payload.get("performance_hints") or {}).items() if k != "dynamic_tempo"}
    if dynamic:
        hints["dynamic_tempo"] = True
    if hints:
        payload["performance_hints"] = hints
    else:
        payload.pop("performance_hints", None)


def _apply_anchored(payload: dict[str, Any], grid: dict[str, Any], beats: Sequence[Mapping[str, Any]], edit: GridEdit) -> None:
    edited, markers = _edited_grid(beats, payload.get("tempo_changes") or [], edit)
    grid["beats"] = edited
    grid["beat_count"] = len(edited)
    grid["static_grid_untrusted"] = True
    payload["tempo_changes"] = markers
    _set_dynamic_tempo_hint(payload, bool(markers))


def apply_to_payload(payload: dict[str, Any], edit: GridEdit, source: GridSource) -> dict[str, Any]:
    """Apply ``edit`` to an ``/anlz`` payload in place and stamp ``beatgrid.edit``.

    The stamp lets a client read the edit in force from the same payload as
    the grid it applies to, never from a second read. It rides INSIDE the
    beatgrid block because every client path that adopts a new grid onto a
    loaded deck replaces that block whole, so the edit can never be left
    describing a grid the deck no longer holds. The identity is NOT stamped:
    an unedited track's payload (and so its ETag) is byte-identical to what it
    was before edits existed, and an absent ``edit`` reads as no edit.
    ``tempo_changes`` markers move with the beats they describe. Tempo anchors
    regenerate the beats, rewrite the markers and mark the grid
    ``static_grid_untrusted`` (GRIDEDIT-10220).
    """
    if edit.is_identity():
        return payload
    grid = dict(payload.get("beatgrid") or {})
    beats = grid.get("beats") or []
    if edit.anchors and beats:
        _apply_anchored(payload, grid, beats, edit)
        grid["edit"] = {"source": source, **edit.as_dict()}
        payload["beatgrid"] = grid
        return payload
    if beats:
        moved = apply_to_beats(beats, edit)
        grid["beats"] = moved
        grid["beat_count"] = len(moved)
    # Markers move with the offset whether or not this payload carries beats,
    # so a stamped edit never leaves them at their measured times (Sol P1, #6235).
    changes = payload.get("tempo_changes")
    if changes and edit.bpm is not None:
        payload["tempo_changes"] = []
    elif changes and edit.offset_ms != 0.0:
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


_REQUIRED_KEYS: Final = frozenset({"offset_ms", "beat_shift"})
# Optional keys compose as required <= keys <= allowed (#10112 adds "bpm" here).
_ALLOWED_KEYS: Final = _REQUIRED_KEYS | {"anchors", "bpm", "anchor_t"}


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _parse_anchors(where: str, raw: object) -> tuple[Anchor, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ANCHORS:
        raise ValueError(f"{where} anchors is not a list of 1..{MAX_ANCHORS}: {raw!r}")
    anchors: list[Anchor] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"at_s", "bpm"}:
            raise ValueError(f"{where} anchor is malformed: {item!r}")
        at_s, bpm = item["at_s"], item["bpm"]
        if not _finite_number(at_s) or not _finite_number(bpm) or not MIN_BPM <= bpm <= MAX_BPM or at_s < 0:
            raise ValueError(f"{where} anchor has a bad time or tempo: {item!r}")
        if anchors and at_s <= anchors[-1].at_s:
            raise ValueError(f"{where} anchors are not strictly increasing in time: {raw!r}")
        anchors.append(Anchor(at_s=float(at_s), bpm=float(bpm)))
    return tuple(anchors)


def _parse_edit(where: str, edit: object) -> GridEdit:
    """One source's stored edit, with no key or type coerced."""
    if not isinstance(edit, dict) or not _REQUIRED_KEYS <= set(edit) <= _ALLOWED_KEYS:
        raise ValueError(f"{where} edit is malformed: {edit!r}")
    offset_ms, beat_shift = edit["offset_ms"], edit["beat_shift"]
    if isinstance(offset_ms, bool) or not isinstance(offset_ms, (int, float)) or not math.isfinite(offset_ms):
        raise ValueError(f"{where} offset_ms is not a finite number: {offset_ms!r}")
    if isinstance(beat_shift, bool) or not isinstance(beat_shift, int):
        raise ValueError(f"{where} beat_shift is not an integer: {beat_shift!r}")
    anchors = _parse_anchors(where, edit["anchors"]) if "anchors" in edit else ()
    bpm = edit.get("bpm")
    anchor_t = edit.get("anchor_t")
    if (bpm is None) != (anchor_t is None):
        raise ValueError(f"{where} bpm and anchor_t must be paired: {edit!r}")
    if bpm is not None and (
        isinstance(bpm, bool)
        or not isinstance(bpm, (int, float))
        or not math.isfinite(bpm)
        or not MIN_BPM <= float(bpm) <= MAX_BPM
    ):
        raise ValueError(f"{where} bpm is out of range or not a finite number: {bpm!r}")
    if anchor_t is not None and (
        isinstance(anchor_t, bool)
        or not isinstance(anchor_t, (int, float))
        or not math.isfinite(anchor_t)
    ):
        raise ValueError(f"{where} anchor_t is not a finite number: {anchor_t!r}")
    return make_edit(
        float(offset_ms),
        beat_shift,
        anchors=anchors,
        bpm=None if bpm is None else float(bpm),
        anchor_t=None if anchor_t is None else float(anchor_t),
    )


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


# ---------- undo (GRIDEDIT-10215) ---------------------------------------------


@dataclass(frozen=True)
class UndoStep:
    """What one undo writes back: every source's edit and the restored edit's own time."""

    edits: dict[GridSource, GridEdit]
    modified_at: str


def _all_identity() -> dict[GridSource, GridEdit]:
    return {src: IDENTITY for src in GRID_SOURCES}


def _same_edits(a: Mapping[str, GridEdit], b: Mapping[str, GridEdit]) -> bool:
    return all(a.get(src, IDENTITY) == b.get(src, IDENTITY) for src in GRID_SOURCES)


def previous(conn: sqlite3.Connection, stable_id: str) -> UndoStep | None:
    """The state one undo returns to, or None when nothing is left to undo.

    States in write order are the history rows (oldest first) then the current
    row. The current ``modified_at`` first appears at state ``p``: the undo
    target is the state before it (the identity before the first write). A
    target equal to the current value is skipped by stepping back again.
    """
    current = conn.execute(
        "SELECT value_json, modified_at FROM track_fields "
        "WHERE stable_id = ? AND field_name = ? AND deleted_at IS NULL",
        (stable_id, FIELD),
    ).fetchone()
    if current is None:
        return None
    history = conn.execute(
        "SELECT value_json, modified_at FROM track_field_history "
        "WHERE stable_id = ? AND field_name = ? ORDER BY id ASC",
        (stable_id, FIELD),
    ).fetchall()
    states: list[tuple[dict[str, GridEdit], str]] = [
        (_parse_blob(stable_id, str(raw)), str(stamp)) for raw, stamp in [*history, current]
    ]
    current_edits = states[-1][0]
    cursor = states[-1][1]
    while True:
        p = next(i for i, (_edits, stamp) in enumerate(states) if stamp == cursor)
        if p == 0:
            target_edits: dict[str, GridEdit] = dict(_all_identity())
            target_stamp = states[0][1]
        else:
            target_edits, target_stamp = states[p - 1]
        if not _same_edits(target_edits, current_edits):
            return UndoStep(
                edits={src: target_edits.get(src, IDENTITY) for src in GRID_SOURCES},
                modified_at=target_stamp,
            )
        if p == 0:
            return None
        cursor = target_stamp


__all__ = [
    "BEATS_PER_BAR",
    "BLOB_VERSION",
    "FIELD",
    "GRID_SOURCES",
    "IDENTITY",
    "MAX_ANCHORS",
    "MAX_BPM",
    "MAX_OFFSET_MS",
    "MIN_BPM",
    "NO_EDITS",
    "OWN_SOURCE",
    "Anchor",
    "GridEdit",
    "GridEditError",
    "GridSource",
    "StoredGridEdits",
    "UndoStep",
    "apply_to_beats",
    "apply_to_payload",
    "blob",
    "make_edit",
    "normalize_beat_shift",
    "nudged",
    "previous",
    "read",
    "rebuild_beats_at_bpm",
    "relabel_beat_number",
    "with_anchor",
    "with_edit",
    "without_anchor",
]
