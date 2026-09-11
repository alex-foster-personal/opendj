"""Plan-building for :mod:`apps.mik.load`: decides every row that would be
written, without writing anything. Split out of ``load.py`` (the file-size
review gate). Purely mechanical: ``load.py`` re-exports ``build_plan``, so
``from apps.mik.load import build_plan`` (the existing import shape) is
unaffected.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
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
    existing: dict[tuple[str, str], str]
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
    if not lookups.allowed[field_name]:
        plan._bump(plan.blocked_by_gate, field_name)
        return
    match = state.match
    if match is None:
        plan.staged.append(
            _stage(state.song, field_name, value, confidence, state.unmatched)
        )
        return
    if _conflicts_with_promotion(lookups.promoted, state.row_id, match.stable_id):
        plan._bump(plan.blocked_by_promotion_conflict, field_name)
        return
    prior_source = lookups.existing.get((match.stable_id, field_name))
    if field_name == "key":
        band = _key_precedence_band(confidence, prior_source)
        if band == _KEY_BLOCKED_FLOOR:
            plan.blocked_by_key_floor += 1
            return
        if band == _KEY_REVIEW_BAND:
            plan.key_review_band.append(match.stable_id)
            return
    if prior_source is not None and prior_source != SOURCE:
        outranks = OUTRANKS_MIK.get(field_name, frozenset())
        if prior_source in outranks or not lookups.overwrite_lower_precedence:
            plan._bump(plan.blocked_by_precedence, field_name)
            return
    plan.field_writes.append(
        FieldWrite(
            stable_id=match.stable_id,
            field_name=field_name,
            value=value,
            confidence=confidence,
            modified_at=state.modified_at,
            tier=match.tier,
        )
    )


def build_plan(
    songs: Iterable[MikSong],
    report: MatchReport,
    gate: EquivalenceGate,
    conn: sqlite3.Connection,
    *,
    overwrite_lower_precedence: bool = False,
) -> LoadPlan:
    """Decide every row that would be written. Pure planning, no writes."""
    songs = list(songs)
    plan = LoadPlan()
    plan.gate_summary = gate.summary(list(GATED_FIELDS))
    plan.verification = gate.provenance_rows(list(GATED_FIELDS))
    allowed = {name: gate.may_write(name) for name in GATED_FIELDS}
    lookups = _PlanLookups(
        allowed=allowed,
        existing=_existing_field_sources(conn, SCALAR_FIELDS),
        promoted=_promoted_row_targets(_promoted_stable_ids(conn, GATED_FIELDS)),
        overwrite_lower_precedence=overwrite_lower_precedence,
    )
    promoted = lookups.promoted

    for song in songs:
        match: Match | None = report.matches.get(song.row_id)
        unmatched: Unmatched | None = report.unmatched.get(song.row_id)
        if match is None and unmatched is None:
            raise LoadError(
                f"MIK row {song.row_id} is in neither matches nor unmatched; "
                f"the match report is incomplete"
            )
        modified_at = song.analysed_at
        if modified_at is None:
            plan._bump(plan.no_value, "analysed_at")
            continue
        row_id = str(song.row_id)
        state = _SongPlanState(
            song=song,
            match=match,
            unmatched=unmatched,
            modified_at=modified_at,
            row_id=row_id,
            scalars=_scalar_values(song),
        )

        for field_name in SCALAR_FIELDS:
            _plan_scalar_field(field_name, state, lookups, plan)

        if not song.segments:
            plan._bump(plan.no_value, SEGMENTS_FIELD)
            continue
        if not allowed[SEGMENTS_FIELD]:
            plan._bump(plan.blocked_by_gate, SEGMENTS_FIELD)
            continue
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
                    unmatched,
                )
            )
            continue
        if _conflicts_with_promotion(promoted, row_id, match.stable_id):
            plan._bump(plan.blocked_by_promotion_conflict, SEGMENTS_FIELD)
            continue
        plan.segment_writes.append(
            SegmentWrite(
                stable_id=match.stable_id,
                segments=song.segments,
                confidence=MIK_CONFIDENCE,
                modified_at=modified_at,
                tier=match.tier,
            )
        )
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
