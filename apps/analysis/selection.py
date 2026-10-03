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
  Rekordbox write-back (later) is gated on the persisted default via
  :func:`lane_is_promoted`, not on :func:`effective_source`.

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

from ._value_check import require_allowed_value
from .canonical import PROJECTION_FIELDS
from .lanes import LANES, Lane
from .serving_lanes import SERVING_LANES, register_serving_lane, serving_lanes

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
    return require_allowed_value(lane, LANES, "lane", "lanes", SelectionError)


def check_source(source: str) -> str:
    return require_allowed_value(
        source, SOURCES, "source", "sources", SelectionError
    )


def check_toggle_state(state: str) -> str:
    return require_allowed_value(
        state, TOGGLE_STATES, "toggle state", "states", SelectionError
    )


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


def lane_is_promoted(conn: sqlite3.Connection, lane: str) -> bool:
    """True iff ``analysis_source_default`` for this lane is ``own``.

    Ignores the PARITY-02 toggle. Later write paths call this, never
    :func:`effective_source`.
    """
    return get_default(conn, lane) == "own"


#-----------------------------------------------------------------------------
# in-memory dev toggle (PARITY-02)
#-----------------------------------------------------------------------------

_TOGGLE_LOCK = threading.Lock()
# Launch state is `unset` for every lane, and this dict is the only place
# it lives. Nothing loads it from disk on purpose: a toggle that could be
# restored would be able to reapply an override a promotion superseded,
# which is the exact failure PARITY-02's launch state exists to prevent.
_TOGGLE: dict[str, ToggleState] = {lane: "unset" for lane in LANES}
# Monotonic counter of successful toggle mutations, launch state 0. A "write"
# is a `write_toggle` call that enters the locked mutation and assigns the
# toggle (a compare-and-set refusal is not a write; an open-time durable
# failure before `write_toggle` runs is not a write; assigning the same value
# again still is). Compensation routed through `write_toggle` counts as
# another write. A VALUE can repeat (own -> rbx -> own reads as "own"
# again); a revision never does, so it is what tells "nothing changed since
# my write" apart from "the value happens to match again"
# (discussion_r3974993963 P1 BLOCKING, issue #3189).
_TOGGLE_REVISION: dict[str, int] = {lane: 0 for lane in LANES}


def get_toggle(lane: str) -> ToggleState:
    _check_lane(lane)
    with _TOGGLE_LOCK:
        return _TOGGLE[lane]


def get_toggle_revision(lane: str) -> int:
    _check_lane(lane)
    with _TOGGLE_LOCK:
        return _TOGGLE_REVISION[lane]


def all_toggle_revisions() -> dict[str, int]:
    with _TOGGLE_LOCK:
        return dict(_TOGGLE_REVISION)


@dataclass(frozen=True)
class ToggleWrite:
    """One toggle mutation's result: the displaced value, the value it set
    and the revision the write landed at, all read under ONE lock
    acquisition so a caller compensating this exact write later has the
    REVISION, not just the value that can repeat after an own -> rbx -> own
    round trip (discussion_r3974993963 P1 BLOCKING), and a caller REPORTING
    this write can describe it without a second, separately-locked read that
    a concurrent write could land before (see `source_state`).
    """

    previous: ToggleState
    current: ToggleState
    revision: int


def write_toggle(
    lane: str,
    new: str,
    *,
    expected: str | None = None,
    expected_revision: int | None = None,
) -> ToggleWrite | None:
    """The one locked primitive `set_toggle` and `compare_and_set_toggle`
    delegate to. Unconditional when `expected` is None; otherwise a
    compare-and-set requiring the CURRENT value to equal `expected` and,
    when `expected_revision` is also given, the CURRENT revision to equal
    it too. `expected_revision` is ignored when `expected` is None.

    Returns the `ToggleWrite` this call produced, or None when a condition
    was given and failed - the toggle is left untouched in that case.
    """
    _check_lane(lane)
    check_toggle_state(new)
    if expected is not None:
        check_toggle_state(expected)
    with _TOGGLE_LOCK:
        current = _TOGGLE[lane]
        if expected is not None and current != expected:
            return None
        if (
            expected is not None
            and expected_revision is not None
            and _TOGGLE_REVISION[lane] != expected_revision
        ):
            return None
        _TOGGLE[lane] = new  # type: ignore[assignment]
        _TOGGLE_REVISION[lane] += 1
        return ToggleWrite(previous=current, current=_TOGGLE[lane], revision=_TOGGLE_REVISION[lane])


def set_toggle(lane: str, state: str) -> ToggleState:
    """Set `lane`'s toggle unconditionally. Returns the value it DISPLACED,
    read and written under the same lock acquisition so it is authoritative
    even when a concurrent PUT landed since the caller's own last read
    (discussion_r3974235454 P1 BLOCKING). Use `write_toggle` directly when
    the resulting REVISION is also needed.
    """
    result = write_toggle(lane, state)
    assert result is not None  # unconditional write never refuses
    return result.previous


def compare_and_set_toggle(
    lane: str, expected: str, new: str, *, expected_revision: int | None = None
) -> bool:
    """Set `lane`'s toggle to `new` only if it currently holds `expected`,
    reading and writing under one lock acquisition (inside `write_toggle`)
    so a compensating rollback (analysis-source.svelte.ts
    `_rollBackFailedSwitch`) can never clobber a concurrent agent's newer
    write with a stale pre-switch value (discussion_r3973129053 P2
    BLOCKING).

    `expected_revision` closes the ABA gap a value-only compare-and-set
    misses: `expected` -> something else -> `expected` round-trips back to
    a value that matches again even though the world moved in between
    (discussion_r3974993963 P1 BLOCKING). A caller holding the revision its
    own prior `write_toggle` landed at can pass it here to require that
    nothing wrote the toggle at all since. Returns whether the set happened.
    """
    result = write_toggle(lane, new, expected=expected, expected_revision=expected_revision)
    return result is not None


def all_toggles() -> dict[str, ToggleState]:
    with _TOGGLE_LOCK:
        return dict(_TOGGLE)


def _toggle_snapshot() -> tuple[dict[str, ToggleState], dict[str, int]]:
    """Every lane's toggle AND revision from one lock acquisition, so a value
    is never paired with a revision some other write produced."""
    with _TOGGLE_LOCK:
        return dict(_TOGGLE), dict(_TOGGLE_REVISION)


def reset_toggles() -> None:
    """Return every lane to its launch state. Used by tests and by nothing else."""
    with _TOGGLE_LOCK:
        for lane in LANES:
            _TOGGLE[lane] = "unset"
            _TOGGLE_REVISION[lane] = 0


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


def effective_source_for_track(
    conn: sqlite3.Connection,
    lane: str,
    *,
    has_rb_mapping: bool,
) -> Source:
    """Per-track effective source (STANDALONE-06).

    PARITY-02 toggle ``rbx``/``own`` wins when set. With toggle ``unset``,
    unmapped tracks default to ``own``; rekordbox-mapped tracks keep the
    persisted per-lane default (NATIVE-14 promotion unchanged).
    """
    _check_lane(lane)
    toggle = get_toggle(lane)
    if toggle != "unset":
        return toggle
    if not has_rb_mapping:
        return "own"
    return get_default(conn, lane)


def implicit_own_default(
    conn: sqlite3.Connection | None,
    lane: str,
    *,
    has_rb_mapping: bool | None,
) -> bool:
    """True when ``own`` is effective for this track ONLY via STANDALONE-06.

    That is an unmapped track, the PARITY-02 toggle ``unset``, and a lane
    default that is still ``rbx`` (no NATIVE-14 promotion). In that state and
    with no own record yet, a reader keeps serving the value the track
    already had (a ``track_fields`` tag or manual value, a locally decoded
    waveform) rather than hiding it behind ``missing`` (STANDALONE-03). An
    explicit ``own`` toggle or a promoted lane still answers ``missing``, so
    no other source is ever substituted under a chosen own selection. With no
    state DB there is no promotion table, which ``get_default`` answers
    ``DEFAULT_SOURCE`` for.
    """
    _check_lane(lane)
    if has_rb_mapping is not False or get_toggle(lane) != "unset":
        return False
    default = get_default(conn, lane) if conn is not None else DEFAULT_SOURCE
    return default == "rbx"


def effective_source_and_implicit_own_for_track(
    conn: sqlite3.Connection,
    lane: str,
    *,
    has_rb_mapping: bool,
) -> tuple[Source, bool]:
    """:func:`effective_source_for_track` and :func:`implicit_own_default` from ONE read.

    Calling the two separately reads the in-process toggle twice, each under its
    own lock acquisition, and the persisted default in separate autocommit
    statements, so a toggle or default write landing between them can pair a
    source with a verdict about a different selection. This reads the toggle
    once and the default at most once, so the pair always describes one state.
    """
    _check_lane(lane)
    toggle = get_toggle(lane)
    if toggle != "unset":
        return toggle, False
    if has_rb_mapping:
        return get_default(conn, lane), False
    return "own", get_default(conn, lane) == "rbx"


def bulk_has_rb_mapping(conn: sqlite3.Connection, stable_ids: list[str]) -> frozenset[str]:
    """``stable_id`` values with a live rekordbox ``track_vendor_ids`` row.

    Does not consult ``djmdContent``; callers on the webui hot path should
    prefer :func:`apps.webui.server.rb_vendor_pkg.track_rows.bulk_rb_meta`
    keys when the master DB is already loaded.
    """
    if not stable_ids or not _table_exists(conn, "track_vendor_ids"):
        return frozenset()
    out: set[str] = set()
    for i in range(0, len(stable_ids), 500):
        sub = stable_ids[i : i + 500]
        placeholders = ",".join("?" * len(sub))
        for row in conn.execute(
            "SELECT stable_id FROM track_vendor_ids "
            "WHERE vendor = 'rekordbox' AND deleted_at IS NULL "
            f"AND stable_id IN ({placeholders})",
            tuple(sub),
        ):
            out.add(str(row[0]))
    return frozenset(out)


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


def source_state(
    conn: sqlite3.Connection, *, written: tuple[str, ToggleWrite] | None = None
) -> dict[str, Any]:
    """The whole selection surface, as the HTTP endpoint and CLI report it.

    `written` pins one lane's row to a `ToggleWrite` the caller just made, so
    a PUT response describes ITS OWN write. The snapshot below is a separate
    lock acquisition from that write, and a concurrent write landing in
    between would otherwise surface as this request's toggle and effective
    source: a switch to own answered with an agent's later `unset`, which
    the client adopts as "no deck disagrees" and resolves a switch it never
    refreshed for (analysis-source.test.mjs "a failed switch never clobbers
    a concurrent agent-driven HTTP change during rollback", about 1 in 25
    runs under load). Patching the revision alone left `toggle` and
    `effective` from the later read, a row no single moment ever held.
    """
    defaults = all_defaults(conn)
    toggles, revisions = _toggle_snapshot()
    if written is not None:
        lane, write = written
        toggles[lane] = write.current
        revisions[lane] = write.revision
    return {
        "lanes": {
            lane: {
                "default": defaults[lane],
                "toggle": toggles[lane],
                "toggle_revision": revisions[lane],
                "effective": toggles[lane] if toggles[lane] != "unset" else defaults[lane],
            }
            for lane in LANES
        },
        "serving": sorted(SERVING_LANES),
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
    status: Literal["ok", "failed", "missing", "available-not-selected"]
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


def _missing_own_field(field_name: str) -> EffectiveField:
    return EffectiveField(
        value=None,
        source=OWN_ANALYSIS_SOURCE,
        confidence=None,
        modified_at="",
        status="missing",
        reason="no own analysis record for this lane yet",
    )


def _implicit_default_track_values(
    conn: sqlite3.Connection,
    field_name: str,
    lane: str,
    own_ids: list[str],
    projected: dict[str, dict[str, EffectiveField]],
) -> dict[str, EffectiveField]:
    """``track_fields`` values an own-resolved field still serves (STANDALONE-03).

    Read only for ids with no projection row, so an own record, ``ok`` or
    ``failed``, always wins; and only under :func:`implicit_own_default`.
    """
    # The lane-level half of the predicate is asked ONCE per lane, not per
    # row: this runs on the library-listing hot path. Under it every id in
    # ``own_ids`` is unmapped by construction, because a mapped track reads
    # the lane default, which the predicate has just confirmed is ``rbx``.
    if field_name not in _RBX_FIELDS or not implicit_own_default(
        conn, lane, has_rb_mapping=False
    ):
        return {}
    ids = [sid for sid in own_ids if projected[sid].get(field_name) is None]
    fetched = _fetch_track_fields(conn, ids, (field_name,))
    return {
        sid: fetched[sid][field_name] for sid in ids if field_name in fetched[sid]
    }


def _annotate_available_not_selected(
    conn: sqlite3.Connection,
    sid: str,
    fields: dict[str, EffectiveField],
    *,
    has_rb_mapping: bool,
    projection_available: bool,
) -> None:
    """When rbx is selected but a canonical own record exists, name it (STANDALONE-03).

    The caller has already established that ``analysis_canonical`` exists and
    passes whether ``analysis_projection`` does: both are schema facts of the
    connection, probed once per call rather than once per track (#3962).
    """
    from .canonical import canonical_pointer

    for field_name, lane in PROJECTION_FIELDS.items():
        if effective_source_for_track(conn, lane, has_rb_mapping=has_rb_mapping) != "rbx":
            continue
        pointer = canonical_pointer(conn, sid, lane)
        if pointer is None:
            continue
        own_row = (
            _fetch_projection(conn, [sid], (field_name,))[sid].get(field_name)
            if projection_available
            else None
        )
        if own_row is None or own_row.status != "ok":
            continue
        served = fields.get(field_name)
        fields[field_name] = EffectiveField(
            value=served.value if served is not None else None,
            source=served.source if served is not None else "rekordbox",
            confidence=served.confidence if served is not None else None,
            modified_at=served.modified_at if served is not None else "",
            status="available-not-selected",
            reason=f"{lane} analysis available (rekordbox source selected)",
        )


def effective_fields(
    conn: sqlite3.Connection,
    stable_ids: Iterable[str],
    selection: Selection,
    *,
    rb_mapped: Mapping[str, bool] | None = None,
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

    When ``rb_mapped`` is supplied, each track's lane sources are resolved
    via :func:`effective_source_for_track` (STANDALONE-06). Otherwise every
    track shares ``selection`` (legacy global read).
    """
    ids = list(stable_ids)
    if not ids:
        return {}

    projection_available = (
        selection.projection_available
        if rb_mapped is None
        else _table_exists(conn, "analysis_projection")
    )
    out: dict[str, dict[str, EffectiveField]] = {sid: {} for sid in ids}

    if rb_mapped is None:
        own_fields = tuple(
            f for f, lane in PROJECTION_FIELDS.items() if selection.source(lane) == "own"
        )
        rbx_fields = tuple(
            f for f, lane in PROJECTION_FIELDS.items()
            if selection.source(lane) == "rbx" and f in _RBX_FIELDS
        )
        out = _fetch_track_fields(conn, ids, rbx_fields)
        projected = (
            _fetch_projection(conn, ids, own_fields)
            if projection_available
            else {sid: {} for sid in ids}
        )
        for sid in ids:
            for field_name in own_fields:
                found = projected[sid].get(field_name)
                out[sid][field_name] = (
                    found if found is not None else _missing_own_field(field_name)
                )
        return out

    for field_name, lane in PROJECTION_FIELDS.items():
        rbx_ids: list[str] = []
        own_ids: list[str] = []
        for sid in ids:
            mapped = bool(rb_mapped.get(sid, False))
            if effective_source_for_track(conn, lane, has_rb_mapping=mapped) == "own":
                own_ids.append(sid)
            elif field_name in _RBX_FIELDS:
                rbx_ids.append(sid)
        if rbx_ids:
            fetched = _fetch_track_fields(conn, rbx_ids, (field_name,))
            for sid in rbx_ids:
                if field_name in fetched[sid]:
                    out[sid][field_name] = fetched[sid][field_name]
        if own_ids:
            projected = (
                _fetch_projection(conn, own_ids, (field_name,))
                if projection_available
                else {sid: {} for sid in own_ids}
            )
            track_values = _implicit_default_track_values(
                conn, field_name, lane, own_ids, projected,
            )
            for sid in own_ids:
                found = projected[sid].get(field_name, track_values.get(sid))
                out[sid][field_name] = (
                    found if found is not None else _missing_own_field(field_name)
                )

    if not _table_exists(conn, "analysis_canonical"):
        return out
    for sid in ids:
        _annotate_available_not_selected(
            conn,
            sid,
            out[sid],
            has_rb_mapping=bool(rb_mapped.get(sid, False)),
            projection_available=projection_available,
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
    "SERVING_LANES",
    "SOURCES",
    "TOGGLE_STATES",
    "EffectiveField",
    "Selection",
    "SelectionError",
    "Source",
    "ToggleState",
    "ToggleWrite",
    "all_defaults",
    "all_toggle_revisions",
    "all_toggles",
    "check_lane",
    "check_source",
    "check_toggle_state",
    "compare_and_set_toggle",
    "bulk_has_rb_mapping",
    "effective_fields",
    "effective_source",
    "effective_source_and_implicit_own_for_track",
    "effective_source_for_track",
    "implicit_own_default",
    "ensure_tables",
    "field_column_sql",
    "get_default",
    "get_toggle",
    "get_toggle_revision",
    "lane_for_field",
    "lane_is_promoted",
    "register_serving_lane",
    "reset_toggles",
    "serving_lanes",
    "set_default",
    "set_toggle",
    "source_state",
    "write_toggle",
]


if __name__ == "__main__":  # pragma: no cover - thin delegation
    import sys

    from .selection_cli import main

    sys.exit(main(sys.argv[1:]))
