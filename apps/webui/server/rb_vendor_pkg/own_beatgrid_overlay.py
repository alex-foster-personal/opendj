"""Own beatgrid projection (native-analysis v1, NATIVE-01/02/03).

`GET /anlz` serves the OWN record for whichever lane's effective source is
own (spec section 3, "Consumer, /anlz lanes"). For `beatgrid` that means the
whole block is replaced: beats with their bar position, the published BPM,
the tempo-change markers and `static_grid_untrusted`, or an explicit
`status: failed` / `status: missing` with a reason. It NEVER means rekordbox
beats appearing under an own selection, which is the exact silent
substitution this milestone exists to remove.

The overlay is applied AFTER the file cache, beside the cues overlay and for
the same reason: `anlz-cache` is keyed on the ANLZ file mtime and the points
parameter, neither of which moves when an own record is written or when the
source toggle flips. A cached own block would go stale in silence.

Own multi-anchor grids are trusted dynamic grids: per-beat local bpm already
carries the tempo map, so `static_grid_untrusted` is omitted on that branch
while `performance_hints.dynamic_tempo` is set from `tempo_changes`.

Split out of `apps/webui/server/rb_vendor_pkg/anlz.py` (which was pushed over
the 600-line file-size ceiling by this block landing inline) into its own
module; the logic is unchanged.

-Claude Sonnet 5
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

SOURCE_OWN = "own"
SOURCE_REKORDBOX = "rekordbox"

OWN_BEATGRID_LANE = "beatgrid"
OWN_BEATGRID_MISSING_REASON = "no own beatgrid record for this track yet"


def _beatgrid_payload(tags: dict[str, Any]) -> tuple[dict[str, Any], list[float]]:
    """The rekordbox-sourced ``beatgrid`` block, straight off the PQTZ tag.

    Lives beside ``SOURCE_REKORDBOX`` rather than in ``anlz.py``: that file was
    pushed over the 600-line ceiling by this feature landing, and this
    function is the other half of the same ``source`` discriminator contract
    as ``apply_own_beatgrid`` below (spec section 3,
    ``specs/native-analysis-v1-lanes/nav1-consumers.md`` item 1).

    Wire shape on both sides is ``{n, bpm, t}``. Fixed-tempo write-back
    (``apps.sync.analysis_writeback_pqtz``, promotion-gated) maps ``n`` to
    PQTZ ``beat`` uint16 in 1..4; ``bpm`` to PQTZ ``tempo`` uint16
    ``int(round(bpm * 100))`` (PQTZ boundary, not ``djmdContent.BPM``); ``t``
    seconds to PQTZ ``time`` uint32 ms ``int(round(t * 1000))``. Read-back
    is this function: ``bpm`` ``round(., 2)``, ``t`` ``round(., 3)``,
    ``zip(..., strict=True)``. ``PQTZAnlzTag.set`` cannot change entry count;
    the writer replaces ``entries`` + ``entry_count`` and ``update_len``.
    Multi-anchor (``tempo_changes`` non-empty) is refused; dynamic PQTZ is
    #1481.
    """
    pqtz = tags.get("PQTZ")
    if pqtz is None:
        return {"source": SOURCE_REKORDBOX, "beat_count": 0, "beats": []}, []
    beats = pqtz.get_beats()
    bpms = pqtz.get_bpms()
    times = [float(t) for t in pqtz.get_times()]
    grid = {
        # The REQUIRED wire discriminator, stated by the rekordbox branch as
        # well as the own one. A consumer that had to infer the source from
        # which other fields happen to be present would relabel an own payload
        # as rekordbox the moment a field was renamed, and hand it quantize and
        # Beat Sync by default (specs/native-analysis-v1-lanes/nav1-consumers.md
        # item 1). A discriminator only one side states is not a discriminator.
        "source": SOURCE_REKORDBOX,
        "beat_count": len(beats),
        "beats": [
            {"n": int(n), "bpm": round(float(bpm), 2), "t": round(t, 3)}
            for n, bpm, t in zip(beats, bpms, times, strict=True)
            # A PQTZ tag with mismatched array lengths is a malformed rekordbox
            # export, not a source this producer half-trusts (Sol P1, PR
            # #1587): silently truncating to the shortest array would drop
            # real beats off a real deck's grid without saying so.
        ],
    }
    return grid, times


def _state_conn_ro(state_db_path: Path | None = None) -> Any:
    """A read-only state connection, or None when there is no state DB at all.

    No state DB means no own record can exist, which has a correct answer
    (`status: missing`) rather than being a failure to paper over. Mirrors the
    guard `track_rows.py` already uses on the same file.

    `state_db_path` defaults to the process-global `config.STATE_DB` so
    existing callers and tests (which monkeypatch that constant) are
    unaffected; a caller with access to `request.app.state.analysis_db_path`
    passes it through explicitly instead (Codex P2 BLOCKING, PR #1587): the
    `/analysis/source` route already honors that override when reading and
    writing the toggle, so this overlay reading the process-global default
    instead could report `effective: own` from one database while serving the
    beatgrid from another.
    """
    from apps.adapters.rekordbox import config

    db_path = state_db_path if state_db_path is not None else config.STATE_DB
    if not db_path.exists():
        return None
    from apps.adapters.rekordbox.errors import _open_ro

    return _open_ro(db_path, "STATE_DB")


def _table_present(conn: Any, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _effective_beatgrid_source(conn: Any) -> str:
    """`rbx` or `own` for the beatgrid lane, with or without a state DB.

    With a connection this is `apps.analysis.selection.effective_source`
    verbatim. Without one there is no `analysis_source_default` table to read,
    which is the same state `get_default` answers `DEFAULT_SOURCE` for, so the
    in-memory toggle is the only input left. Spelled out rather than routed
    through a fabricated connection so the no-database case is visible.
    """
    from apps.analysis import selection

    if conn is not None:
        return selection.effective_source(conn, OWN_BEATGRID_LANE)
    toggle = selection.get_toggle(OWN_BEATGRID_LANE)
    return selection.DEFAULT_SOURCE if toggle == "unset" else toggle


def _own_beatgrid_lane_result(conn: Any, stable_id: str) -> Any:
    """The canonical own `beatgrid` lane block for this track, or None.

    None means no own record: the queue is the fix, not a tooltip, and the
    caller renders `status: missing`. A pointer that names a row which is not
    there, or a row that does not carry the lane the pointer named it for, is
    corruption and raises rather than degrading to `missing`.

    `analysis_canonical` absent entirely is NOT that case (Sol P1, PR #1587):
    it is created by the same idempotent `_ensure_analysis_tables` call as
    `analysis_source_default` (apps/analysis/store.py), so the two tables are
    always present or always absent together, never one without the other.
    `apps.analysis.selection.get_default` already rules on that shared state
    ("an absent table means no lane was ever promoted, which is a state with
    a correct answer, not a failure to paper over"); the sibling table
    disagreeing would make two reads of one atomic schema answer differently
    for the identical database. A state DB with no analysis schema at all
    reaches here only through the in-memory toggle (`set_toggle`, a dev/test
    affordance), since promotion itself writes through `open_conn` and would
    have created both tables already.
    """
    if conn is None or not _table_present(conn, "analysis_canonical"):
        return None
    from apps.analysis.canonical import canonical_pointer
    from apps.analysis.record import AnalysisRecord

    pointer = canonical_pointer(conn, stable_id, OWN_BEATGRID_LANE)
    if pointer is None:
        return None
    row = conn.execute(
        "SELECT record_json FROM analysis "
        "WHERE stable_id = ? AND backend = ? AND backend_version = ?",
        (stable_id, pointer[0], pointer[1]),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"canonical beatgrid pointer for {stable_id} names {pointer[0]}@"
            f"{pointer[1]} but no such analysis row exists"
        )
    result = AnalysisRecord.from_json(row[0]).lanes.get(OWN_BEATGRID_LANE)
    if result is None:
        # Mirrors apps.analysis.canonical.rebuild_projection's identical guard:
        # `_eligible_rows` only ever points a lane's canonical pointer at a row
        # whose backend was parsed as THAT lane, so a pointed-to record missing
        # its own named lane is the schema disagreeing with itself, not an
        # ordinary "not analyzed yet" state.
        raise RuntimeError(
            f"canonical beatgrid record {pointer[0]}@{pointer[1]} for "
            f"{stable_id} carries no {OWN_BEATGRID_LANE!r} lane"
        )
    return result


def _own_beatgrid_block(result: Any, stable_id: str) -> tuple[dict[str, Any], list[Any]]:
    """`(beatgrid block, tempo_changes)` for an own-selected track.

    `tempo_changes` is a TOP-LEVEL payload field and is empty for every state
    except a successful lane that found markers.
    """
    if result is None:
        return {
            "source": SOURCE_OWN, "status": "missing",
            "reason": OWN_BEATGRID_MISSING_REASON,
            "beat_count": 0, "beats": [],
        }, []
    if result.status != "ok":
        return {
            "source": SOURCE_OWN, "status": result.status, "reason": result.reason,
            "beat_count": 0, "beats": [],
        }, []

    payload = result.payload
    beats = payload["beats"]
    if not beats:
        # Spec section 3: an empty `beatgrid.beats` with `status: ok` is a
        # contract violation, not a state. The store's own validator refuses to
        # write one; this is the read side saying so rather than serving a
        # healthy-looking empty grid that would silently disable the deck.
        raise RuntimeError(
            f"own beatgrid record for {stable_id} is status ok with an empty "
            "beats array, which is a contract violation and not a state"
        )
    block: dict[str, Any] = {
        "source": SOURCE_OWN,
        "status": "ok",
        "reason": None,
        "beat_count": len(beats),
        "beats": [
            {"n": int(b["n"]), "bpm": float(b["bpm"]), "t": float(b["t"])}
            for b in beats
        ],
        "bpm": float(payload["bpm"]),
    }
    tempo_changes = list(payload["tempo_changes"])
    if not tempo_changes:
        block["static_grid_untrusted"] = bool(payload["static_grid_untrusted"])
    return block, tempo_changes


def apply_own_beatgrid(
    payload: dict[str, Any],
    stable_id: str,
    state_db_path: Path | None = None,
    *,
    has_rb_mapping: bool | None = None,
) -> dict[str, Any]:
    """Replace the beatgrid block with the own record when own is selected.

    A no-op that returns the payload unchanged when the effective source is
    rekordbox, which is the launch state for every lane until a promotion
    (D1). Mutates and returns ``payload``.

    ``state_db_path`` is forwarded to `_state_conn_ro`; see its docstring.
    """
    from .own_lane_store import effective_lane_source

    conn = _state_conn_ro(state_db_path)
    try:
        if effective_lane_source(conn, OWN_BEATGRID_LANE, has_rb_mapping=has_rb_mapping) != SOURCE_OWN:
            return payload
        result = _own_beatgrid_lane_result(conn, stable_id)
    finally:
        if conn is not None:
            conn.close()

    block, tempo_changes = _own_beatgrid_block(result, stable_id)
    payload["beatgrid"] = block
    payload["tempo_changes"] = tempo_changes
    _set_dynamic_tempo_hint(payload, present=bool(tempo_changes))
    return payload


def _set_dynamic_tempo_hint(payload: dict[str, Any], *, present: bool) -> None:
    """Make `performance_hints.dynamic_tempo` say what the OWN record says.

    RECONCILED FOR EVERY OUTCOME, not only set when markers exist. Once the
    lane is on own, the own record is the authority for this field, and the
    payload it lands on may already carry a `dynamic_tempo: true` from a
    cached entry written while a DIFFERENT record was canonical. Setting the
    hint on the dynamic branch and leaving the static, failed and missing
    branches alone let that stale `true` survive, so an own grid the analyzer
    measured as static would be served as dynamic (Sol P1 BLOCKING, PR #1587).
    So the flag is either set or REMOVED, never inherited.

    Removal rather than `false`: absent means "not analyzed" to the readers in
    `lib/rb/anlz-types.ts`, and a rekordbox payload has no such key at all, so
    writing `false` here would give the same field two different meanings
    depending on which branch produced it.

    A NEW dict rather than a mutation of whatever was there: the cached branch
    hands this function a SHALLOW copy of the cache entry, so mutating a
    nested dict in place would write into the cached object itself and leave
    it there for the next request.
    """
    existing = payload.get("performance_hints")
    hints = dict(existing) if isinstance(existing, dict) else {}
    if present:
        hints["dynamic_tempo"] = True
    else:
        hints.pop("dynamic_tempo", None)
    if hints:
        payload["performance_hints"] = hints
    else:
        # An empty hints object is not a state either: it would read as
        # "analyzed, nothing found" where the contract says absence means the
        # analyzer has not spoken.
        payload.pop("performance_hints", None)


__all__ = ["_beatgrid_payload", "apply_own_beatgrid"]
