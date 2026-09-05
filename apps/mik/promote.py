"""Promotion: ``unmatched_source_analysis`` -> ``track_fields`` (+ segments).

The staging table only earns its keep if there is a documented, tested way OUT
of it. That is this module. It is the answer to "what happens when the file
comes back, or when a later ingest finally creates the track row".

The unit of promotion is a SOURCE ROW, not a single staged field: all of MIK
song 4212's fields belong to the same track or to none of them. Promoting per
field would let ``key`` land on one track and ``energy`` on another.

Guarantees, all covered by ``tests/mik/test_promote.py``:

* **Idempotent.** Promoting the same source row to the same stable_id twice
  writes nothing the second time and reports ``already_promoted``.
* **Conflict is loud.** Promoting a row that was already promoted to a
  DIFFERENT stable_id raises :class:`PromotionConflict`. A silent re-point
  would rewrite history invisibly.
* **Still gated.** Promotion goes through the same
  :class:`~apps.shared.equivalence.EquivalenceGate` as the initial load, so a
  field that was staged under an override cannot sneak into ``track_fields``
  later without its own passing verdict.
* **Still precedence-checked.** Promotion applies the same precedence policy
  as :func:`apps.mik.load.build_plan`: a track that picked up a field from a
  higher-precedence source while its MIK analysis sat in staging keeps that
  value. Key uses the three-band confidence policy; every other scalar checks
  :data:`apps.mik.load.OUTRANKS_MIK`.
* **The staged row is kept.** Promotion stamps ``promoted_stable_id`` and
  ``promoted_at``; it never deletes. The staging table doubles as the import
  audit trail.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from apps.shared.equivalence import EquivalenceGate
from apps.shared.state import provenance as _prov

from . import SOURCE
from .load import (
    KEY_CONFIDENCE_FLOOR,
    KEY_CONFIDENCE_MIK_WINS,
    OUTRANKS_MIK,
    SEGMENTS_FIELD,
    record_verification,
)
from .match import TIER_ORDER, TIER_RANK, Tier, TrackIndex

log = logging.getLogger(__name__)


class PromotionConflict(RuntimeError):
    """A staged row is already promoted to a different track."""


class PromotionError(RuntimeError):
    """The promotion cannot proceed safely."""


@dataclass
class PromotionResult:
    source_row_id: str
    stable_id: str
    fields_written: int = 0
    segment_rows: int = 0
    already_promoted: int = 0
    blocked_by_gate: list[str] = field(default_factory=list)
    blocked_by_precedence: list[str] = field(default_factory=list)
    blocked_by_key_floor: int = 0
    key_review_band: list[str] = field(default_factory=list)
    verification_rows: int = 0


@dataclass(frozen=True)
class StagedRow:
    id: int
    source: str
    source_row_id: str
    field_name: str
    value_json: str
    confidence: float | None
    modified_at: str
    title: str | None
    artist: str | None
    source_path: str | None
    promoted_stable_id: str | None
    promoted_at: str | None


def _row(record: tuple) -> StagedRow:
    return StagedRow(
        id=record[0],
        source=record[1],
        source_row_id=record[2],
        field_name=record[3],
        value_json=record[4],
        confidence=record[5],
        modified_at=record[6],
        title=record[7],
        artist=record[8],
        source_path=record[9],
        promoted_stable_id=record[10],
        promoted_at=record[11],
    )


_SELECT = (
    "SELECT id, source, source_row_id, field_name, value_json, confidence, "
    "modified_at, title, artist, source_path, promoted_stable_id, promoted_at "
    "FROM unmatched_source_analysis"
)


def staged_rows(
    conn: sqlite3.Connection,
    *,
    source: str = SOURCE,
    source_row_id: str | None = None,
    pending_only: bool = False,
) -> list[StagedRow]:
    sql = f"{_SELECT} WHERE source = ?"
    params: list[object] = [source]
    if source_row_id is not None:
        sql += " AND source_row_id = ?"
        params.append(source_row_id)
    if pending_only:
        sql += " AND promoted_stable_id IS NULL"
    sql += " ORDER BY source_row_id, field_name"
    return [_row(record) for record in conn.execute(sql, params)]


def pending_counts(conn: sqlite3.Connection, *, source: str = SOURCE) -> dict[str, int]:
    """``{unmatched_reason: distinct source rows}`` still un-promoted."""
    return {
        reason: count
        for reason, count in conn.execute(
            "SELECT unmatched_reason, COUNT(DISTINCT source_row_id) "
            "FROM unmatched_source_analysis "
            "WHERE source = ? AND promoted_stable_id IS NULL "
            "GROUP BY unmatched_reason",
            (source,),
        )
    }


# ------------------------------------------------------------- discovery


def discover(
    conn: sqlite3.Connection,
    *,
    source: str = SOURCE,
    allow_fuzzy: bool = False,
) -> dict[str, str]:
    """Re-match pending staged rows against ``tracks`` as it is NOW.

    Returns ``{source_row_id: stable_id}`` for rows that now have exactly one
    candidate in exactly one tier. Defaults to ``allow_fuzzy=False`` because a
    row landed in staging precisely BECAUSE fuzzy matching could not place it;
    re-running the same fuzzy tiers would mostly re-derive the same ambiguity.

    Several staged rows can independently discover the SAME stable_id (e.g.
    two staged rows sharing a path, which now resolves to one newly created
    track). Assigning that stable_id to more than one caller here would let
    :func:`promote_all` write both -- and since both are the same source, the
    second is not precedence-blocked, so it silently overwrites the first's
    scalar and segment analysis, with the final values depending on
    dict/sort order rather than any real ranking (P1 regression, PR #383
    review). So claimants of one stable_id are ranked the same way
    :func:`apps.mik.match.match_songs` resolves the identical collision
    shape -- strongest tier, then highest KEY confidence, then lowest
    source_row_id -- and every loser is left staged (``pending_only``) for a
    human or a future disambiguating signal, rather than silently promoted.
    Key confidence specifically (not ``StagedRow.confidence`` off whichever
    field happens to sort first): every OTHER scalar field is staged with the
    flat :data:`apps.mik.load.MIK_CONFIDENCE` constant, identical for every
    song, so it carries no ranking signal at all -- ``key`` is the only field
    whose ``confidence`` is the song's real, per-row ``key_confidence``,
    which is exactly what :func:`apps.mik.match.match_songs` ranks its own
    identical collision on.
    """
    index = TrackIndex.from_conn(conn)
    tiers: tuple[Tier, ...] = TIER_ORDER if allow_fuzzy else ("exact_path",)
    identities: dict[str, StagedRow] = {}
    key_confidence: dict[str, float | None] = {}
    for row in staged_rows(conn, source=source, pending_only=True):
        identities.setdefault(row.source_row_id, row)
        if row.field_name == "key":
            key_confidence[row.source_row_id] = row.confidence
    provisional: dict[str, tuple[str, Tier]] = {}  # source_row_id -> (stable_id, tier)
    for source_row_id, row in identities.items():
        for tier in tiers:
            candidates = index.candidates_for(
                tier, path=row.source_path, artist=row.artist, title=row.title
            )
            if len(candidates) == 1:
                provisional[source_row_id] = (candidates[0], tier)
                break
            if candidates:
                break  # ambiguous in the strongest tier that hit: leave staged

    claimants: dict[str, list[str]] = {}
    for source_row_id, (stable_id, _tier) in provisional.items():
        claimants.setdefault(stable_id, []).append(source_row_id)

    def _rank(source_row_id: str) -> tuple[int, float, str]:
        _stable_id, tier = provisional[source_row_id]
        confidence = key_confidence.get(source_row_id)
        return (
            TIER_RANK[tier],
            -(confidence if confidence is not None else -1.0),
            source_row_id,
        )

    found: dict[str, str] = {}
    for stable_id, source_row_ids in claimants.items():
        winner = min(source_row_ids, key=_rank)
        found[winner] = stable_id
    return found


# ------------------------------------------------------------- promoting

_BLOCKED_KEY_FLOOR = "key_floor"
_BLOCKED_KEY_REVIEW_BAND = "key_review_band"
_BLOCKED_PRECEDENCE = "precedence"


def _precedence_block(
    conn: sqlite3.Connection,
    row: StagedRow,
    *,
    stable_id: str,
    source: str,
    overwrite_lower_precedence: bool,
) -> str | None:
    """Mirror :func:`apps.mik.load.build_plan`'s precedence policy at
    promotion time. Returns which bucket blocks the write, or ``None`` if
    the field may be written.
    """
    prior = conn.execute(
        "SELECT source FROM track_fields WHERE stable_id = ? AND field_name = ?",
        (stable_id, row.field_name),
    ).fetchone()
    prior_source = prior[0] if prior is not None else None
    if row.field_name == "key":
        if row.confidence is None or row.confidence < KEY_CONFIDENCE_FLOOR:
            # rekordbox wins outright below the floor.
            return _BLOCKED_KEY_FLOOR
        if (
            row.confidence < KEY_CONFIDENCE_MIK_WINS
            and prior_source is not None
            and prior_source != source
        ):
            # The 0.70-0.90 band: FLAG for review rather than silently
            # guessing which of two plausible keys is right.
            return _BLOCKED_KEY_REVIEW_BAND
    if prior_source is not None and prior_source != source:
        outranks = OUTRANKS_MIK.get(row.field_name, frozenset())
        if prior_source in outranks or not overwrite_lower_precedence:
            return _BLOCKED_PRECEDENCE
    return None


@dataclass(frozen=True)
class _PromotionCtx:
    """Bundles one ``promote()`` call's fixed arguments for ``_promote_row``."""

    gate: EquivalenceGate
    stable_id: str
    source: str
    actor: str
    stamp: str
    overwrite_lower_precedence: bool


def _promote_row(
    conn: sqlite3.Connection, row: StagedRow, ctx: _PromotionCtx, result: PromotionResult
) -> None:
    """Promote one staged field of a source row, updating ``result`` in place."""
    # A rescan can update value_json/confidence/modified_at on an
    # already-promoted row while leaving promoted_stable_id untouched (it
    # stays pointed at the same target). Only skip the re-check when the
    # staged payload has not changed since it was last promoted; otherwise
    # the target keeps stale data forever, because every later promotion
    # attempt would return here too.
    if row.promoted_stable_id == ctx.stable_id and (
        row.promoted_at is not None and row.modified_at <= row.promoted_at
    ):
        result.already_promoted += 1
        return
    if not ctx.gate.may_write(row.field_name):
        result.blocked_by_gate.append(row.field_name)
        return
    value = json.loads(row.value_json)
    if row.field_name == SEGMENTS_FIELD:
        result.segment_rows += _write_segments(
            conn,
            stable_id=ctx.stable_id,
            segments=value,
            confidence=row.confidence,
            modified_at=row.modified_at,
        )
    else:
        block = _precedence_block(
            conn,
            row,
            stable_id=ctx.stable_id,
            source=ctx.source,
            overwrite_lower_precedence=ctx.overwrite_lower_precedence,
        )
        if block == _BLOCKED_KEY_FLOOR:
            result.blocked_by_key_floor += 1
            return
        if block == _BLOCKED_KEY_REVIEW_BAND:
            result.key_review_band.append(ctx.stable_id)
            return
        if block == _BLOCKED_PRECEDENCE:
            result.blocked_by_precedence.append(row.field_name)
            return
        _prov.write_field(
            conn,
            stable_id=ctx.stable_id,
            field_name=row.field_name,
            value=value,
            source=ctx.source,  # type: ignore[arg-type]
            modified_at=row.modified_at,
            confidence=row.confidence,
            actor=ctx.actor,
            now=ctx.stamp,
        )
        result.fields_written += 1
    conn.execute(
        "UPDATE unmatched_source_analysis SET promoted_stable_id = ?, "
        "promoted_at = ? WHERE id = ?",
        (ctx.stable_id, ctx.stamp, row.id),
    )


def promote(
    conn: sqlite3.Connection,
    gate: EquivalenceGate,
    *,
    source_row_id: str,
    stable_id: str,
    source: str = SOURCE,
    now: str | None = None,
    actor: str = "apps.mik.promote",
    overwrite_lower_precedence: bool = False,
) -> PromotionResult:
    """Promote every staged field of one source row onto ``stable_id``.

    A scalar field also goes through the SAME precedence policy as
    :func:`apps.mik.load.build_plan`: a track that acquired a field from
    another source between staging and promotion must not have it clobbered
    just because the staged row finally found its track. Key uses the
    three-band confidence policy; every other scalar checks
    :data:`apps.mik.load.OUTRANKS_MIK`. A blocked field is left staged
    (``promoted_stable_id`` untouched) so a later promotion attempt -- after
    the blocking field is deleted, or with ``overwrite_lower_precedence`` --
    can still succeed. This mirrors ``blocked_by_gate``: precedence, like the
    equivalence gate, is a promotion-time property, not a staging-time one.

    Also persists this call's field-verification provenance via
    :func:`apps.mik.load.record_verification`, for the same reason the
    initial load does: a field promoted from staging needs the same
    "how was this verified" audit trail as one written straight through.

    Caller owns the transaction: this runs inside a SAVEPOINT so a batch can be
    committed or rolled back as a unit.
    """
    stamp = now or datetime.now(UTC).isoformat()
    rows = staged_rows(conn, source=source, source_row_id=source_row_id)
    if not rows:
        raise PromotionError(
            f"no staged rows for source={source!r} source_row_id={source_row_id!r}"
        )
    known = conn.execute(
        "SELECT 1 FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
        (stable_id,),
    ).fetchone()
    if known is None:
        raise PromotionError(
            f"stable_id {stable_id!r} is not in tracks (or is soft-deleted); "
            f"promotion would create an orphan field row, or resurrect "
            f"analysis onto a tombstoned track"
        )
    for row in rows:
        if row.promoted_stable_id is not None and row.promoted_stable_id != stable_id:
            raise PromotionConflict(
                f"staged row {row.id} ({row.field_name}) is already promoted to "
                f"{row.promoted_stable_id!r}, refusing to re-point it to "
                f"{stable_id!r}"
            )

    result = PromotionResult(source_row_id=source_row_id, stable_id=stable_id)
    savepoint = f"promote_{abs(hash((source, source_row_id, stamp)))}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        field_names = sorted({row.field_name for row in rows})
        result.verification_rows = record_verification(
            conn, gate.provenance_rows(field_names), source=source, now=stamp
        )
        ctx = _PromotionCtx(
            gate=gate,
            stable_id=stable_id,
            source=source,
            actor=actor,
            stamp=stamp,
            overwrite_lower_precedence=overwrite_lower_precedence,
        )
        for row in rows:
            _promote_row(conn, row, ctx, result)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    return result


def _write_segments(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    segments: object,
    confidence: float | None,
    modified_at: str,
) -> int:
    if not isinstance(segments, list) or not segments:
        raise PromotionError(
            f"staged {SEGMENTS_FIELD} for {stable_id} is not a non-empty list"
        )
    conn.execute(
        "DELETE FROM track_energy_segments WHERE stable_id = ? AND source = ?",
        (stable_id, SOURCE),
    )
    ordered = sorted(segments, key=lambda item: int(item["start_ms"]))
    conn.executemany(
        "INSERT INTO track_energy_segments(stable_id, seq, start_ms, length_ms, "
        "energy, source, confidence, start_clamped, modified_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                stable_id,
                seq,
                int(item["start_ms"]),
                int(item["length_ms"]),
                int(item["energy"]),
                SOURCE,
                confidence,
                int(item.get("start_clamped", False)),
                modified_at,
            )
            for seq, item in enumerate(ordered)
        ],
    )
    return len(ordered)


def promote_all(
    conn: sqlite3.Connection,
    gate: EquivalenceGate,
    assignments: dict[str, str],
    *,
    source: str = SOURCE,
    now: str | None = None,
    overwrite_lower_precedence: bool = False,
) -> list[PromotionResult]:
    """Promote many source rows in ONE transaction. All or nothing."""
    stamp = now or datetime.now(UTC).isoformat()
    results: list[PromotionResult] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for source_row_id, stable_id in sorted(assignments.items()):
            results.append(
                promote(
                    conn,
                    gate,
                    source_row_id=source_row_id,
                    stable_id=stable_id,
                    source=source,
                    now=stamp,
                    overwrite_lower_precedence=overwrite_lower_precedence,
                )
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return results


__all__ = [
    "PromotionConflict",
    "PromotionError",
    "PromotionResult",
    "StagedRow",
    "discover",
    "pending_counts",
    "promote",
    "promote_all",
    "staged_rows",
]
