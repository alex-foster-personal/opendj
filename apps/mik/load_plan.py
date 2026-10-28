"""Plan-building for :mod:`apps.mik.load`: decides every row that would be
written, without writing anything. Split out of ``load.py`` (the file-size
review gate). Purely mechanical: ``load.py`` re-exports ``build_plan``, so
``from apps.mik.load import build_plan`` (the existing import shape) is
unaffected.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from apps.shared.equivalence import EquivalenceGate

from . import SOURCE
from .load_types import (
    GATED_FIELDS,
    KEY_CONFIDENCE_FLOOR,
    KEY_CONFIDENCE_MIK_WINS,
    MIK_CONFIDENCE,
    OUTRANKS_MIK,
    SCALAR_FIELDS,
    SEGMENTS_FIELD,
    FieldVersion,
    FieldWrite,
    LoadError,
    LoadPlan,
    SegmentWrite,
    StagedWrite,
)
from .match import Match, MatchReport, Unmatched
from .mikdb import MikSong


def _existing_field_sources(
    conn: sqlite3.Connection, field_names: Iterable[str]
) -> dict[tuple[str, str], str]:
    """``{(stable_id, field_name): source}`` for rows already in track_fields."""
    names = tuple(field_names)
    if not names:
        return {}
    placeholders = ",".join("?" for _ in names)
    return {
        (stable_id, field_name): source
        for stable_id, field_name, source in conn.execute(
            f"SELECT stable_id, field_name, source FROM track_fields "
            f"WHERE field_name IN ({placeholders})",
            names,
        )
    }


def _field_versions(
    conn: sqlite3.Connection, field_names: Iterable[str]
) -> dict[tuple[str, str], FieldVersion]:
    """``{(stable_id, field_name): (source, value_json, modified_at)}`` for rows in track_fields."""
    names = tuple(field_names)
    if not names:
        return {}
    placeholders = ",".join("?" for _ in names)
    return {
        (stable_id, field_name): (source, value_json, modified_at)
        for stable_id, field_name, source, value_json, modified_at in conn.execute(
            f"SELECT stable_id, field_name, source, value_json, modified_at FROM track_fields "
            f"WHERE field_name IN ({placeholders})",
            names,
        )
    }


def staged_versions(
    conn: sqlite3.Connection, keys: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], tuple[Any, ...] | None]:
    """``{(source_row_id, field_name): every column the staging upsert replaces, or None}``."""
    return {
        (row_id, field_name): conn.execute(
            "SELECT value_json, unmatched_reason, confidence, title, artist, album, duration_ms, "
            "source_path, modified_at FROM unmatched_source_analysis "
            "WHERE source = 'mik' AND source_row_id = ? AND field_name = ?",
            (row_id, field_name),
        ).fetchone()
        for row_id, field_name in keys
    }


def _mik_series_analyses(conn: sqlite3.Connection) -> dict[str, frozenset[str]]:
    """Each row's MIK series analysis times. Another source's series is no MIK analysis."""
    found: dict[str, set[str]] = {}
    query = "SELECT DISTINCT stable_id, modified_at FROM track_energy_segments WHERE source = 'mik'"
    for stable_id, modified_at in conn.execute(query):
        found.setdefault(stable_id, set()).add(modified_at)
    return {stable_id: frozenset(times) for stable_id, times in found.items()}


def current_promotions(conn: sqlite3.Connection) -> dict[str, str]:
    """Each promoted MIK source row's authoritative target track."""
    return _promoted_row_targets(_promoted_stable_ids(conn, GATED_FIELDS))


def _record_planned_versions(conn: sqlite3.Connection, plan: LoadPlan) -> None:
    """What every staged row, series target and promotion looks like now, for the lock-time check."""
    plan.promotions = current_promotions(conn)
    plan.staged_versions = staged_versions(conn, {(write.source_row_id, write.field_name) for write in plan.staged})
    plan.series_versions = {write.stable_id: series_version(conn, write.stable_id) for write in plan.segment_writes}


def series_version(conn: sqlite3.Connection, stable_id: str) -> tuple[tuple[Any, ...], ...]:
    """The stored MIK energy series of ``stable_id``, row by row; empty when it has none."""
    return tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT seq, start_ms, length_ms, energy, confidence, start_clamped, modified_at "
            "FROM track_energy_segments WHERE stable_id = ? AND source = 'mik' ORDER BY seq",
            (stable_id,),
        )
    )


def _promoted_stable_ids(
    conn: sqlite3.Connection, field_names: Iterable[str]
) -> dict[tuple[str, str], str]:
    """``{(source_row_id, field_name): promoted_stable_id}`` for MIK rows a
    prior :func:`apps.mik.promote.promote` already bound to a track.

    A library change (rescan, re-ingest) can let the normal matcher pair an
    already-promoted source row with a DIFFERENT track than the one it was
    promoted to. Without this lookup ``build_plan`` would happily write the
    same analysis onto the new match while the promoted track keeps its own
    copy, attaching one source analysis to two identities and bypassing
    :class:`apps.mik.promote.PromotionConflict` entirely.
    """
    names = tuple(field_names)
    if not names:
        return {}
    placeholders = ",".join("?" for _ in names)
    return {
        (source_row_id, field_name): promoted_stable_id
        for source_row_id, field_name, promoted_stable_id in conn.execute(
            f"SELECT source_row_id, field_name, promoted_stable_id "
            f"FROM unmatched_source_analysis "
            f"WHERE source = ? AND field_name IN ({placeholders}) "
            f"AND promoted_stable_id IS NOT NULL",
            (SOURCE, *names),
        )
    }


def _scalar_values(song: MikSong) -> dict[str, tuple[Any, float | None]]:
    """MIK's scalars as ``{field_name: (value, confidence)}``, absent ones omitted."""
    out: dict[str, tuple[Any, float | None]] = {}
    if song.key_camelot is not None:
        out["key"] = (song.key_camelot, song.key_confidence)
    if song.energy is not None:
        out["energy"] = (song.energy, MIK_CONFIDENCE)
    if song.bpm is not None:
        out["bpm"] = (song.bpm, MIK_CONFIDENCE)
    if song.loudness is not None:
        out["loudness"] = (song.loudness, MIK_CONFIDENCE)
    if song.clipped_peak_count is not None:
        out["clipped_peak_count"] = (song.clipped_peak_count, MIK_CONFIDENCE)
    return out


_KEY_BLOCKED_FLOOR = "blocked_floor"
_KEY_REVIEW_BAND = "review_band"
_KEY_PROCEED = "proceed"


def _key_precedence_band(confidence: float | None, prior_source: str | None) -> str:
    """Where a MIK key value lands: below the confidence floor (rekordbox
    wins outright), the 0.70-0.90 review band (flagged, never silently
    guessed), or clear to go through the normal precedence check."""
    if confidence is None or confidence < KEY_CONFIDENCE_FLOOR:
        return _KEY_BLOCKED_FLOOR
    if (
        confidence < KEY_CONFIDENCE_MIK_WINS
        and prior_source is not None
        and prior_source != SOURCE
    ):
        return _KEY_REVIEW_BAND
    return _KEY_PROCEED


def _promoted_row_targets(promoted: dict[tuple[str, str], str]) -> dict[str, str]:
    """Collapse the per-``(row_id, field_name)`` promotion bindings down to
    one authoritative target per source row.

    A promotion event binds every field that cleared the gate to the SAME
    stable_id in one transaction (see :mod:`apps.mik.promote`); a field that
    was instead blocked (e.g. by the key-confidence floor) simply has no
    binding of its own. So a promoted SIBLING field is already proof of the
    row's target, and every field of that row -- promoted or not -- must be
    checked against it, not just the field that happens to carry a binding.
    """
    targets: dict[str, str] = {}
    for (row_id, _field_name), stable_id in promoted.items():
        targets[row_id] = stable_id
    return targets


def _conflicts_with_promotion(
    promoted_targets: dict[str, str], row_id: str, stable_id: str
) -> bool:
    """True when this source row was already promoted (on any field) to a
    DIFFERENT track than the one it now matches (see
    :func:`_promoted_row_targets`)."""
    promoted_stable_id = promoted_targets.get(row_id)
    return promoted_stable_id is not None and promoted_stable_id != stable_id


@dataclass(frozen=True)
class _PlanLookups:
    """Read-only, precomputed once per :func:`build_plan` call and shared by
    every song/field decision -- bundled so a per-field helper does not need
    a parameter per lookup table."""

    allowed: dict[str, bool]
    selected: frozenset[str]
    existing: dict[tuple[str, str], str]
    versions: dict[tuple[str, str], FieldVersion]
    series_analyses: dict[str, frozenset[str]]
    promoted: dict[str, str]
    overwrite_lower_precedence: bool


@dataclass(frozen=True)
class _SongPlanState:
    """One song's per-loop-iteration state, bundled for the same reason as
    :class:`_PlanLookups`."""

    song: MikSong
    match: Match | None
    unmatched: Unmatched | None
    modified_at: str
    row_id: str
    scalars: dict[str, tuple[Any, float | None]]
    shared_target: bool = False
    """True for a duplicate the song FILLS: its promotion target binds the
    song's own direct match, never the duplicates it explicitly shares to."""


def _blocked_by_precedence(
    field_name: str,
    confidence: float | None,
    prior_source: str | None,
    match: Match,
    lookups: _PlanLookups,
    plan: LoadPlan,
) -> bool:
    """True, with the reason counted, when a matched field must not be written:
    the key bands first, then a source that outranks MIK or that MIK was not
    asked to overwrite. Split out of :func:`_plan_scalar_field` (complexity)."""
    if field_name == "key":
        band = _key_precedence_band(confidence, prior_source)
        if band == _KEY_BLOCKED_FLOOR:
            plan.blocked_by_key_floor += 1
            return True
        if band == _KEY_REVIEW_BAND:
            plan.key_review_band.append(match.stable_id)
            return True
    if prior_source is None or prior_source == SOURCE:
        return False
    outranks = OUTRANKS_MIK.get(field_name, frozenset())
    if prior_source in outranks or not lookups.overwrite_lower_precedence:
        plan._bump(plan.blocked_by_precedence, field_name)
        return True
    return False


def _plan_scalar_field(
    field_name: str, state: _SongPlanState, lookups: _PlanLookups, plan: LoadPlan
) -> None:
    """Decide the one write, staged row, or block reason for one scalar field
    of one song. Split out of :func:`build_plan` so its own branching does
    not count against that function's per-song loop.
    """
    if field_name not in state.scalars:
        plan._bump(plan.no_value, field_name)
        return
    value, confidence = state.scalars[field_name]
    if field_name not in lookups.selected:
        plan._bump(plan.not_selected, field_name)
        return
    if not lookups.allowed[field_name]:
        plan._bump(plan.blocked_by_gate, field_name)
        return
    match = state.match
    if match is None:
        plan.staged.append(
            _stage(state.song, field_name, value, confidence, state.unmatched)
        )
        return
    if not state.shared_target and _conflicts_with_promotion(lookups.promoted, state.row_id, match.stable_id):
        plan._bump(plan.blocked_by_promotion_conflict, field_name)
        return
    prior_source = lookups.existing.get((match.stable_id, field_name))
    if _blocked_by_precedence(field_name, confidence, prior_source, match, lookups, plan):
        return
    plan.field_writes.append(
        FieldWrite(
            stable_id=match.stable_id,
            field_name=field_name,
            value=value,
            confidence=confidence,
            modified_at=state.modified_at,
            tier=match.tier,
            planned_version=lookups.versions.get((match.stable_id, field_name)),
        )
    )


SHARED_FIELDS: tuple[str, ...] = ("energy", SEGMENTS_FIELD)
"""The fields a duplicate copy of one recording receives (see
:mod:`apps.mik.duplicates`). Nothing else is shared across rows."""


def _plan_segments(state: _SongPlanState, lookups: _PlanLookups, plan: LoadPlan) -> None:
    """The one write, staged row, or block reason for a song's energy series."""
    song, match = state.song, state.match
    if not song.segments:
        plan._bump(plan.no_value, SEGMENTS_FIELD)
        return
    if SEGMENTS_FIELD not in lookups.selected:
        plan._bump(plan.not_selected, SEGMENTS_FIELD)
        return
    if not lookups.allowed[SEGMENTS_FIELD]:
        plan._bump(plan.blocked_by_gate, SEGMENTS_FIELD)
        return
    if match is None:
        plan.staged.append(
            _stage(
                song,
                SEGMENTS_FIELD,
                [
                    {
                        "start_ms": seg.start_ms,
                        "length_ms": seg.length_ms,
                        "energy": seg.energy,
                        "start_clamped": seg.start_clamped,
                    }
                    for seg in song.segments
                ],
                MIK_CONFIDENCE,
                state.unmatched,
            )
        )
        return
    if not state.shared_target and _conflicts_with_promotion(lookups.promoted, state.row_id, match.stable_id):
        plan._bump(plan.blocked_by_promotion_conflict, SEGMENTS_FIELD)
        return
    plan.segment_writes.append(
        SegmentWrite(
            stable_id=match.stable_id,
            segments=song.segments,
            confidence=MIK_CONFIDENCE,
            modified_at=state.modified_at,
            tier=match.tier,
        )
    )


def _shares_despite_promotion(state: _SongPlanState, lookups: _PlanLookups) -> bool:
    """A promoted source row shares only when its direct match is its promotion target."""
    promoted_to = lookups.promoted.get(state.row_id)
    return promoted_to is None or (state.match is not None and state.match.stable_id == promoted_to)


def _holds_own_mik_analysis(stable_id: str, lookups: _PlanLookups) -> bool:
    """A duplicate is FILLED, never overwritten: its own COMPLETE MIK analysis.

    Complete means a MIK value AND a MIK series from the SAME analysis (equal
    ``modified_at``). A value without its series, a series without its value,
    or a pair from two analyses is a row still needing the rest of one, so
    the paired scalar-and-series refresh may complete it; otherwise it would
    sit at ``needs_series`` for good.
    """
    version = lookups.versions.get((stable_id, "energy"))
    return version is not None and version[0] == SOURCE and lookups.series_analyses.get(stable_id) == frozenset({version[2]})


def _plan_one_fill(row_state: _SongPlanState, lookups: _PlanLookups, plan: LoadPlan) -> None:
    """The scalar AND its series for one duplicate, or neither (counted as incomplete)."""
    fields_before, segments_before = len(plan.field_writes), len(plan.segment_writes)
    if "energy" in row_state.scalars and "energy" in lookups.selected:
        _plan_scalar_field("energy", row_state, lookups, plan)
    if row_state.song.segments and SEGMENTS_FIELD in lookups.selected:
        _plan_segments(row_state, lookups, plan)
    # Half a fill leaves the row non-empty, so no later share would complete it.
    wrote_scalar = len(plan.field_writes) > fields_before
    wrote_series = len(plan.segment_writes) > segments_before
    if wrote_scalar != wrote_series:
        plan.shared_incomplete += 1
    if not (wrote_scalar and wrote_series):
        del plan.field_writes[fields_before:]
        del plan.segment_writes[segments_before:]


def _plan_shared(
    state: _SongPlanState, extra: Sequence[Match], lookups: _PlanLookups, plan: LoadPlan
) -> None:
    """Plan :data:`SHARED_FIELDS` once per duplicate row this song also feeds.

    Each row goes through the same gate and precedence checks as a direct
    match; a field with no value or not selected was already counted for the
    song itself, so it is not counted again here. The promotion check is made
    ONCE, on the song's own home, so a misbound source row shares nothing.
    """
    if extra and not _shares_despite_promotion(state, lookups):
        plan._bump(plan.blocked_by_promotion_conflict, "shared_fills")  # the direct write was counted already
        return
    for match in extra:
        if _holds_own_mik_analysis(match.stable_id, lookups):
            plan.shared_has_own += 1
            continue
        _plan_one_fill(replace(state, match=match, unmatched=None, shared_target=True), lookups, plan)


def _song_state(song: MikSong, report: MatchReport, plan: LoadPlan) -> _SongPlanState | None:
    """One song's planning state, or ``None`` (counted) when it has no analysis date."""
    match: Match | None = report.matches.get(song.row_id)
    unmatched: Unmatched | None = report.unmatched.get(song.row_id)
    if match is None and unmatched is None:
        raise LoadError(
            f"MIK row {song.row_id} is in neither matches nor unmatched; "
            f"the match report is incomplete"
        )
    if song.analysed_at is None:
        plan._bump(plan.no_value, "analysed_at")
        return None
    return _SongPlanState(
        song=song,
        match=match,
        unmatched=unmatched,
        modified_at=song.analysed_at,
        row_id=str(song.row_id),
        scalars=_scalar_values(song),
    )


def build_plan(
    songs: Iterable[MikSong],
    report: MatchReport,
    gate: EquivalenceGate,
    conn: sqlite3.Connection,
    *,
    overwrite_lower_precedence: bool = False,
    fields: frozenset[str] | None = None,
    shared: Mapping[int, Sequence[Match]] | None = None,
) -> LoadPlan:
    """Decide every row that would be written. Pure planning, no writes.

    ``fields`` narrows the load to the named gated fields; ``None`` means all
    of them. An unselected field is counted in ``plan.not_selected`` and is
    neither written nor staged, whatever its gate verdict.

    ``shared`` maps a MIK row id to extra rows (duplicate copies of the same
    recording) that receive its :data:`SHARED_FIELDS`. An UNMATCHED song that
    shares does not also stage those fields: they reached a row.
    """
    selected = frozenset(GATED_FIELDS) if fields is None else fields
    unknown = selected - frozenset(GATED_FIELDS)
    if unknown:
        raise LoadError(f"unknown load field(s): {', '.join(sorted(unknown))}")
    songs = list(songs)
    plan = LoadPlan(overwrite_lower_precedence=overwrite_lower_precedence)
    plan.gate_summary = gate.summary(list(GATED_FIELDS))
    plan.verification = gate.provenance_rows(list(GATED_FIELDS))
    allowed = {name: gate.may_write(name) for name in GATED_FIELDS}
    lookups = _PlanLookups(
        allowed=allowed,
        selected=selected,
        existing=_existing_field_sources(conn, SCALAR_FIELDS),
        versions=_field_versions(conn, SCALAR_FIELDS),
        series_analyses=_mik_series_analyses(conn),
        promoted=_promoted_row_targets(_promoted_stable_ids(conn, GATED_FIELDS)),
        overwrite_lower_precedence=overwrite_lower_precedence,
    )
    extra_rows = shared or {}

    for song in songs:
        state = _song_state(song, report, plan)
        if state is None:
            continue
        extra = extra_rows.get(song.row_id, ())
        delivered = SHARED_FIELDS if (state.match is None and extra) else ()
        for field_name in SCALAR_FIELDS:
            if field_name not in delivered:
                _plan_scalar_field(field_name, state, lookups, plan)
        if SEGMENTS_FIELD not in delivered:
            _plan_segments(state, lookups, plan)
        _plan_shared(state, extra, lookups, plan)
    _record_planned_versions(conn, plan)
    return plan


def _stage(
    song: MikSong,
    field_name: str,
    value: Any,
    confidence: float | None,
    unmatched: Unmatched | None,
) -> StagedWrite:
    if unmatched is None:
        raise LoadError(
            f"MIK row {song.row_id} has no match and no unmatched record"
        )
    return StagedWrite(
        source_row_id=str(song.row_id),
        field_name=field_name,
        value=value,
        confidence=confidence,
        unmatched_reason=unmatched.reason,
        title=song.stripped_title,
        artist=song.artist,
        album=song.album,
        duration_ms=None,  # MIK stores no duration column.
        source_path=song.path,
        modified_at=song.analysed_at or "",
    )
