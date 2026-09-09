"""Per-lane source selection (rbx vs own) and the one scalar read function.

`specs/native-analysis-v1.md` section 3, "Effective source":

    Effective source per lane = the PARITY-02 dev toggle if it is set to
    ``rbx`` or ``own``, else the persisted per-lane default (D1). The
    toggle is in-memory, has three states (``unset``, ``rbx``, ``own``),
    starts every launch as ``unset``, and every state is settable and
    readable over HTTP; ``unset`` is the only launch state, so no launch
    ever reapplies an override that a promotion has superseded.

Two pieces of state, deliberately different in kind:

* The DEFAULT is persisted in ``analysis_source_default`` and is what a
  promotion writes. It survives a relaunch, which is the whole point: a
  lane promoted to own must still be own tomorrow.
* The TOGGLE is process-local and in-memory. It is a dev/testing
  affordance for live A/B, so it cannot outlive its session and it cannot
  undo a promotion by launching as ``rbx``. It launches ``unset``.

`effective_fields` is the second half: the ONE function every scalar
reader goes through, so the track list, the browser row hydration, the CLI
and the smartlist evaluator cannot disagree about a track's effective key.
It reads ``track_fields`` for a lane on rbx and ``analysis_projection``
for a lane on own, and it NEVER writes: no own value enters
``track_fields`` or ``track_field_history``, so a session-only toggle
cannot leave a trace in the library or the sync path.

-Claude
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .canonical import PROJECTION_FIELDS
from .lanes import LANES, Lane

Source = Literal["rbx", "own"]
SOURCES: tuple[Source, ...] = ("rbx", "own")

ToggleState = Literal["unset", "rbx", "own"]
TOGGLE_STATES: tuple[ToggleState, ...] = ("unset", "rbx", "own")

# Until a lane is promoted, rekordbox stays the source (D1).
DEFAULT_SOURCE: Source = "rbx"

# Fields a lane on rbx can actually serve. rekordbox stores no loudness
# column and no key-change or tempo-change count, so those fields are
# ABSENT under rbx rather than present-and-null: an absent field reads as
# "this source does not have this", which is true, while a null one reads
# as "measured and empty", which is not.
_RBX_FIELDS: frozenset[str] = frozenset({"bpm", "key"})

# The `source` reported for an own lane that has no record yet. A real
# own value reports its canonical backend name instead.
OWN_ANALYSIS_SOURCE = "own-analysis"



class SelectionError(ValueError):
    """An unknown lane, source, or toggle state."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def check_lane(lane: str) -> str:
    if lane not in LANES:
        raise SelectionError(f"unknown lane {lane!r}; lanes are {LANES}")
    return lane


def check_source(source: str) -> str:
    if source not in SOURCES:
        raise SelectionError(f"unknown source {source!r}; sources are {SOURCES}")
    return source


def check_toggle_state(state: str) -> str:
    if state not in TOGGLE_STATES:
        raise SelectionError(
            f"unknown toggle state {state!r}; states are {TOGGLE_STATES}"
        )
    return state


# Kept as the internal spelling used throughout this module.
_check_lane = check_lane


#-----------------------------------------------------------------------------
# persisted per-lane default
#-----------------------------------------------------------------------------

def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the analysis-domain tables. WRITE path only, see :func:`get_default`.

    Delegates to :mod:`apps.analysis.store`, which is the schema authority
    for this domain. A second CREATE TABLE here would be a durable
    configuration table the authority does not declare, and therefore one
    that adoption, the consolidated-schema drift checks and the database
    documentation could not see.
    """
    from .store import _ensure_analysis_tables

    _ensure_analysis_tables(conn)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def get_default(conn: sqlite3.Connection, lane: str) -> Source:
    """This lane's persisted default, or ``rbx`` when nothing was promoted.

    Deliberately does NOT create the table: the track read path calls this
    on a read-only connection, and a CREATE TABLE there would turn every
    listing into an error. An absent table means no lane was ever promoted,
    which is a state with a correct answer, not a failure to paper over.
    """
    _check_lane(lane)
    if not _table_exists(conn, "analysis_source_default"):
        return DEFAULT_SOURCE
    row = conn.execute(
        "SELECT source FROM analysis_source_default WHERE lane = ?", (lane,)
    ).fetchone()
    if row is None:
        return DEFAULT_SOURCE
    source = row[0]
    if source not in SOURCES:
        raise SelectionError(
            f"analysis_source_default holds {source!r} for lane {lane!r}; "
            f"sources are {SOURCES}"
        )
    return source


def set_default(conn: sqlite3.Connection, lane: str, source: str) -> Source:
    _check_lane(lane)
    check_source(source)
    ensure_tables(conn)
    conn.execute(
        """
        INSERT INTO analysis_source_default (lane, source, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(lane) DO UPDATE SET source = excluded.source,
                                        updated_at = excluded.updated_at
        """,
        (lane, source, _now_iso()),
    )
    return source  # type: ignore[return-value]


def all_defaults(conn: sqlite3.Connection) -> dict[str, Source]:
    return {lane: get_default(conn, lane) for lane in LANES}


#-----------------------------------------------------------------------------
# in-memory dev toggle (PARITY-02)
#-----------------------------------------------------------------------------

_TOGGLE_LOCK = threading.Lock()
# Launch state is `unset` for every lane, and this dict is the only place
# it lives. Nothing loads it from disk on purpose: a toggle that could be
# restored would be able to reapply an override a promotion superseded,
# which is the exact failure PARITY-02's launch state exists to prevent.
_TOGGLE: dict[str, ToggleState] = {lane: "unset" for lane in LANES}


def get_toggle(lane: str) -> ToggleState:
    _check_lane(lane)
    with _TOGGLE_LOCK:
        return _TOGGLE[lane]


def set_toggle(lane: str, state: str) -> ToggleState:
    _check_lane(lane)
    check_toggle_state(state)
    with _TOGGLE_LOCK:
        _TOGGLE[lane] = state  # type: ignore[assignment]
    return state  # type: ignore[return-value]


def compare_and_set_toggle(lane: str, expected: str, new: str) -> bool:
    """Set `lane`'s toggle to `new` only if it currently holds `expected`.

    The one client of this is a compensating rollback (analysis-source.svelte.ts
    `_rollBackFailedSwitch`) putting a failed switch's displaced toggle back. A
    plain read-then-`set_toggle` leaves a window between the client's read and
    its write for a concurrent agent's own PUT to land, and the rollback would
    then clobber that newer value with the stale pre-switch one
    (discussion_r3973129053 P2 BLOCKING). Reading and writing under the SAME
    lock acquisition is what closes that window server-side, where no client
    round trip can reopen it. Returns whether the set happened.
    """
    _check_lane(lane)
    check_toggle_state(expected)
    check_toggle_state(new)
    with _TOGGLE_LOCK:
        if _TOGGLE[lane] != expected:
            return False
        _TOGGLE[lane] = new  # type: ignore[assignment]
        return True


def all_toggles() -> dict[str, ToggleState]:
    with _TOGGLE_LOCK:
        return dict(_TOGGLE)


def reset_toggles() -> None:
    """Return every lane to its launch state. Used by tests and by nothing else."""
    with _TOGGLE_LOCK:
        for lane in LANES:
            _TOGGLE[lane] = "unset"


#-----------------------------------------------------------------------------
# effective source
#-----------------------------------------------------------------------------

def effective_source(conn: sqlite3.Connection, lane: str) -> Source:
    """Toggle when it is set, otherwise the persisted default."""
    _check_lane(lane)
    toggle = get_toggle(lane)
    if toggle == "unset":
        return get_default(conn, lane)
    return toggle


@dataclass(frozen=True)
class Selection:
    """One resolved read context: lane -> source, plus whether own data can exist.

    ``projection_available`` is part of the context and not a lookup done
    later, because both readers need the SAME answer. `analysis_projection`
    is created by :mod:`apps.analysis.store` on its first own write, so a
    lane can be promoted to own on a database that has never held an own
    record. When that happens the honest answer is that own has NO rows:
    the track read model shows `missing`, and a smartlist predicate matches
    nothing. What must NOT happen is either reader falling back to
    rekordbox, or a smartlist raising `no such table`, both of which this
    field's absence caused (Codex P1, PR #1549).
    """

    by_lane: Mapping[str, Source]
    projection_available: bool = False

    def source(self, lane: str) -> Source:
        _check_lane(lane)
        return self.by_lane[lane]

    @property
    def any_own(self) -> bool:
        return any(self.by_lane[lane] == "own" for lane in LANES)

    @classmethod
    def resolve(cls, conn: sqlite3.Connection) -> Selection:
        return cls(
            by_lane={lane: effective_source(conn, lane) for lane in LANES},
            projection_available=_table_exists(conn, "analysis_projection"),
        )

    @classmethod
    def all_rbx(cls) -> Selection:
        return cls(by_lane={lane: "rbx" for lane in LANES})


def source_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """The whole selection surface, as the HTTP endpoint and CLI report it."""
    defaults = all_defaults(conn)
    toggles = all_toggles()
    return {
        "lanes": {
            lane: {
                "default": defaults[lane],
                "toggle": toggles[lane],
                "effective": toggles[lane] if toggles[lane] != "unset" else defaults[lane],
            }
            for lane in LANES
        }
    }


#-----------------------------------------------------------------------------
# the one scalar read
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class EffectiveField:
    """One field's effective value plus the status the read model needs.

    ``status`` is never defaulted anywhere it crosses a boundary: a failed
    own lane must be distinguishable from a value, and from a track that
    was never analyzed.
    """

    value: Any
    source: str
    confidence: float | None
    modified_at: str
    status: Literal["ok", "failed", "missing"]
    reason: str | None = None


def _fetch_track_fields(
    conn: sqlite3.Connection, stable_ids: list[str], fields: tuple[str, ...],
) -> dict[str, dict[str, EffectiveField]]:
    out: dict[str, dict[str, EffectiveField]] = {sid: {} for sid in stable_ids}
    if not fields:
        return out
    field_placeholders = ",".join("?" * len(fields))
    for i in range(0, len(stable_ids), 500):
        sub = stable_ids[i:i + 500]
        placeholders = ",".join("?" * len(sub))
        rows = conn.execute(
            f"SELECT stable_id, field_name, value_json, source, confidence, modified_at "
            f"FROM track_fields WHERE stable_id IN ({placeholders}) "
            f"AND field_name IN ({field_placeholders})",
            (*sub, *fields),
        )
        for row in rows:
            try:
                value = json.loads(row[2])
            except (json.JSONDecodeError, TypeError):
                value = row[2]
            out[row[0]][row[1]] = EffectiveField(
                value=value, source=row[3], confidence=row[4],
                modified_at=row[5], status="ok", reason=None,
            )
    return out


def _fetch_projection(
    conn: sqlite3.Connection, stable_ids: list[str], fields: tuple[str, ...],
) -> dict[str, dict[str, EffectiveField]]:
    out: dict[str, dict[str, EffectiveField]] = {sid: {} for sid in stable_ids}
    if not fields:
        return out
    field_placeholders = ",".join("?" * len(fields))
    for i in range(0, len(stable_ids), 500):
        sub = stable_ids[i:i + 500]
        placeholders = ",".join("?" * len(sub))
        rows = conn.execute(
            f"SELECT stable_id, field, value, status, reason, confidence, "
            f"       backend, updated_at "
            f"FROM analysis_projection WHERE stable_id IN ({placeholders}) "
            f"AND field IN ({field_placeholders})",
            (*sub, *fields),
        )
        for row in rows:
            out[row[0]][row[1]] = EffectiveField(
                value=row[2], source=row[6], confidence=row[5],
                modified_at=row[7], status=row[3], reason=row[4],
            )
    return out


def effective_fields(
    conn: sqlite3.Connection,
    stable_ids: Iterable[str],
    selection: Selection,
) -> dict[str, dict[str, EffectiveField]]:
    """Per track, per lane-owned field: the value the app must read.

    ``track_fields`` for a lane on rbx, ``analysis_projection`` for a lane
    on own. An own lane with no projection row yields ``status: missing``
    rather than the rekordbox value, because falling back would be exactly
    the silent substitution the milestone exists to remove; an rbx lane
    with no ``track_fields`` row yields nothing, which is how an
    unanalyzed track already reads.

    Fields rekordbox cannot serve (``loudness_lufs``, ``loudness_dbtp``,
    ``key_change_count``, ``tempo_change_count``) are absent under rbx.
    """
    ids = list(stable_ids)
    if not ids:
        return {}

    own_fields = tuple(
        f for f, lane in PROJECTION_FIELDS.items() if selection.source(lane) == "own"
    )
    rbx_fields = tuple(
        f for f, lane in PROJECTION_FIELDS.items()
        if selection.source(lane) == "rbx" and f in _RBX_FIELDS
    )

    out = _fetch_track_fields(conn, ids, rbx_fields)
    # No own store means no own records have ever been written. That is ZERO
    # ROWS, so every own field below resolves to `missing`, which is true.
    # It is NOT a reason to serve the rekordbox value.
    projected = (
        _fetch_projection(conn, ids, own_fields)
        if selection.projection_available
        else {sid: {} for sid in ids}
    )
    for sid in ids:
        for field_name in own_fields:
            found = projected[sid].get(field_name)
            out[sid][field_name] = found if found is not None else EffectiveField(
                value=None, source=OWN_ANALYSIS_SOURCE, confidence=None,
                modified_at="", status="missing",
                reason="no own analysis record for this lane yet",
            )
    return out


def field_column_sql(field_name: str, selection: Selection, *, table: str = "tracks") -> str:
    """The SQL twin of :func:`effective_fields` for one lane-owned field.

    A smartlist filter is a query, not a row fetch, so it cannot call
    :func:`effective_fields` and still push the predicate into SQLite.
    What it CAN do is derive its column expression from the same module,
    the same :class:`Selection` and the same table-choice rule, which is
    what makes "the track list and the smartlist evaluator agree on a
    track's effective key" a property of the code rather than a hope.

    Returns a correlated subquery against ``analysis_projection`` for a
    lane on own and against ``track_fields`` for a lane on rbx. A field
    rekordbox cannot serve returns literal ``NULL`` under rbx, so a filter
    on it matches nothing instead of matching the wrong column.
    """
    lane = lane_for_field(field_name)
    source = selection.source(lane)
    if source == "own":
        if not selection.projection_available:
            # No own store, so no own rows: the predicate matches NOTHING.
            # Emitting the subquery anyway would raise `no such table` and
            # take the whole smartlist down; emitting the rekordbox column
            # would answer a question nobody asked.
            return "NULL"
        return (
            "(SELECT ap.value FROM analysis_projection ap "
            f"WHERE ap.stable_id = {table}.stable_id "
            f"AND ap.field = {_sql_text(field_name)} "
            "AND ap.status = 'ok' LIMIT 1)"
        )
    if field_name not in _RBX_FIELDS:
        return "NULL"
    return (
        "(SELECT json_extract(tf.value_json, '$') FROM track_fields tf "
        f"WHERE tf.stable_id = {table}.stable_id "
        f"AND tf.field_name = {_sql_text(field_name)} LIMIT 1)"
    )


def _sql_text(value: str) -> str:
    """Quote an identifier-shaped constant. Refuses anything else."""
    if not value.replace("_", "").isalnum():
        raise SelectionError(f"refusing unsafe SQL literal {value!r}")
    return "'" + value + "'"


def lane_for_field(field_name: str) -> Lane:
    lane = PROJECTION_FIELDS.get(field_name)
    if lane is None:
        raise SelectionError(
            f"{field_name!r} is not a lane-owned field; lane fields are "
            f"{sorted(PROJECTION_FIELDS)}"
        )
    return lane  # type: ignore[return-value]


__all__ = [
    "DEFAULT_SOURCE",
    "LANES",
    "OWN_ANALYSIS_SOURCE",
    "PROJECTION_FIELDS",
    "SOURCES",
    "TOGGLE_STATES",
    "EffectiveField",
    "Selection",
    "SelectionError",
    "Source",
    "ToggleState",
    "all_defaults",
    "all_toggles",
    "check_lane",
    "check_source",
    "check_toggle_state",
    "compare_and_set_toggle",
    "effective_fields",
    "effective_source",
    "ensure_tables",
    "field_column_sql",
    "get_default",
    "get_toggle",
    "lane_for_field",
    "reset_toggles",
    "set_default",
    "set_toggle",
    "source_state",
]


if __name__ == "__main__":  # pragma: no cover - thin delegation
    import sys

    from .selection_cli import main

    sys.exit(main(sys.argv[1:]))
