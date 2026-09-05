"""The gated MIK loader: plan first, write only what has been proven.

Three destinations, chosen by whether we know which track the analysis belongs
to (see ``docs/analysis-retention.md``):

==========================  ==================================================
destination                 for
==========================  ==================================================
``track_fields``            scalars for a MATCHED track. One place, whether or
                            not its audio is on disk -- availability is a
                            separate dimension, not a separate copy
``track_energy_segments``   the energy TIME SERIES for a matched track, in
                            MILLISECONDS
``unmatched_source_analysis``  everything for a source row with no stable_id.
                            Staging, never truth, with a documented promotion
                            path in :mod:`apps.mik.promote`
==========================  ==================================================

**Two gates, both fail-closed.**

1. *Equivalence* (:mod:`apps.shared.equivalence`). A field whose semantic
   equivalence has not been PROVEN is not written anywhere -- not even into
   staging. Rationale: an unverified value in the DB is worse than no value,
   because the next reader cannot tell them apart, and re-reading MIK is cheap
   (read-only source, always available), so waiting costs nothing. With no
   verdict file present, this loader writes ZERO rows unless
   ``--i-know-equivalence-is-unverified`` is passed, which logs loudly per
   field.

2. *Precedence*. ``track_fields`` is keyed ``(stable_id, field_name)`` with no
   source in the key, so writing MIK's ``bpm`` would CLOBBER rekordbox's. MIK
   loses on BPM (6.4% outright disagreement plus 3.2% half/double errors,
   MIK-AUDIT section 3), so by default this loader only FILLS a field no other
   source has supplied. Key is a THREE-band policy rather than a single floor,
   because the only peer-reviewed benchmark of commercial key detection ranks
   rekordbox ABOVE MIK: at ``ZCONFIDENCE >= 0.90`` MIK may win (with
   ``--overwrite-lower-precedence``), the ``0.70-0.90`` band is FLAGGED for
   review rather than silently guessed, and below ``0.70`` rekordbox wins
   outright.

Dry-run is the default everywhere. ``--live`` writes.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from apps.shared.state import provenance as _prov

from . import SOURCE
from .load_plan import build_plan
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

log = logging.getLogger(__name__)

# ------------------------------------------------------------ applying


@dataclass
class ApplyResult:
    fields_written: int = 0
    fields_unchanged: int = 0
    segment_tracks: int = 0
    segment_rows: int = 0
    segment_tracks_unchanged: int = 0
    segment_rows_unchanged: int = 0
    staged_written: int = 0
    staged_unchanged: int = 0
    verification_rows: int = 0


def record_verification(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    source: str = SOURCE,
    now: str | None = None,
) -> int:
    """Persist HOW each field was verified, next to the values it produced.

    Cross-source agreement and a one-sided single-source probe are not the same
    strength of evidence, and a bare ``passed`` in a log line does not survive
    six months. This writes the basis into the DB so a later reader cannot
    mistake one for the other. Idempotent: re-running replaces the row.

    ``rows`` comes from
    :meth:`apps.shared.equivalence.EquivalenceGate.provenance_rows`, captured
    at plan time so a dry-run can show exactly what would be recorded.
    """
    stamp = now or datetime.now(UTC).isoformat()
    conn.executemany(
        "INSERT INTO analysis_field_verification(source, field_name, status, "
        "basis, normaliser, checked_at, verified_by, overridden, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source, field_name) DO UPDATE SET status=excluded.status, "
        "basis=excluded.basis, normaliser=excluded.normaliser, "
        "checked_at=excluded.checked_at, verified_by=excluded.verified_by, "
        "overridden=excluded.overridden, recorded_at=excluded.recorded_at",
        [
            (
                source,
                row["field_name"],
                row["status"],
                row["basis"],
                row["normaliser"],
                row["checked_at"],
                row["verified_by"],
                int(row["overridden"]),
                stamp,
            )
            for row in rows
        ],
    )
    return len(rows)


def _series_matches(conn: sqlite3.Connection, write: SegmentWrite) -> bool:
    """True if the stored series for this (track, source) is already identical.

    Without this a re-run would DELETE and re-INSERT 37k rows to reach the
    state it was already in: idempotent in outcome but not in work, and it
    churns the WAL for nothing.
    """
    stored = conn.execute(
        "SELECT seq, start_ms, length_ms, energy, confidence, start_clamped, "
        "modified_at FROM track_energy_segments "
        "WHERE stable_id = ? AND source = ? ORDER BY seq",
        (write.stable_id, SOURCE),
    ).fetchall()
    if len(stored) != len(write.segments):
        return False
    for row, seg in zip(stored, write.segments, strict=True):
        if tuple(row) != (
            seg.seq,
            seg.start_ms,
            seg.length_ms,
            seg.energy,
            write.confidence,
            int(seg.start_clamped),
            write.modified_at,
        ):
            return False
    return True


def apply_plan(
    conn: sqlite3.Connection,
    plan: LoadPlan,
    *,
    now: str | None = None,
    actor: str = "apps.mik.load",
) -> ApplyResult:
    """Write ``plan`` inside ONE transaction. All or nothing.

    ``track_fields`` goes through :func:`apps.shared.state.provenance.write_field`
    so history, the ``events`` row and byte-equal no-op detection all behave
    exactly as they do for every other writer. The v4 tables are not
    provenance-wrapped EAV rows, so they are written here directly and summarised
    with one ``mik.load`` event rather than 39k per-row events.
    """
    stamp = now or datetime.now(UTC).isoformat()
    result = ApplyResult()
    conn.execute("BEGIN IMMEDIATE")
    try:
        result.verification_rows = record_verification(
            conn, plan.verification, now=stamp
        )
        for write in plan.field_writes:
            changed = _prov.write_field(
                conn,
                stable_id=write.stable_id,
                field_name=write.field_name,
                value=write.value,
                source=SOURCE,  # type: ignore[arg-type]
                modified_at=write.modified_at,
                confidence=write.confidence,
                actor=f"{actor}:{write.tier}",
                now=stamp,
            )
            if changed:
                result.fields_written += 1
            else:
                result.fields_unchanged += 1

        for segment_write in plan.segment_writes:
            if _series_matches(conn, segment_write):
                result.segment_tracks_unchanged += 1
                result.segment_rows_unchanged += len(segment_write.segments)
                continue
            # Replace the whole series for this (track, source): a partial
            # overlay of an old and a new analysis would be a chimera.
            conn.execute(
                "DELETE FROM track_energy_segments WHERE stable_id = ? AND source = ?",
                (segment_write.stable_id, SOURCE),
            )
            conn.executemany(
                "INSERT INTO track_energy_segments(stable_id, seq, start_ms, "
                "length_ms, energy, source, confidence, start_clamped, "
                "modified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        segment_write.stable_id,
                        seg.seq,
                        seg.start_ms,
                        seg.length_ms,
                        seg.energy,
                        SOURCE,
                        segment_write.confidence,
                        int(seg.start_clamped),
                        segment_write.modified_at,
                    )
                    for seg in segment_write.segments
                ],
            )
            result.segment_tracks += 1
            result.segment_rows += len(segment_write.segments)

        for staged in plan.staged:
            cursor = conn.execute(
                "INSERT INTO unmatched_source_analysis(source, source_row_id, "
                "field_name, value_json, unmatched_reason, confidence, title, "
                "artist, album, isrc, duration_ms, source_path, modified_at, "
                "imported_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?) "
                "ON CONFLICT(source, source_row_id, field_name) DO UPDATE SET "
                "value_json=excluded.value_json, "
                "unmatched_reason=excluded.unmatched_reason, "
                "confidence=excluded.confidence, title=excluded.title, "
                "artist=excluded.artist, album=excluded.album, "
                "duration_ms=excluded.duration_ms, "
                "source_path=excluded.source_path, "
                "modified_at=excluded.modified_at, imported_at=excluded.imported_at "
                # Every mutable column the SET clause above touches must be
                # compared here, or a change the SET applies never reaches
                # rowcount and reads as unchanged. Nullable columns (all but
                # value_json/unmatched_reason/modified_at) use IS NOT: '<>'
                # against SQL NULL evaluates to NULL, not true, so a genuine
                # NULL<->value transition would otherwise pass silently.
                # imported_at is deliberately excluded: it is this run's own
                # write-time stamp, not a MIK fact, so it must not force a
                # write on its own.
                "WHERE unmatched_source_analysis.value_json <> excluded.value_json "
                "   OR unmatched_source_analysis.unmatched_reason "
                "      <> excluded.unmatched_reason "
                "   OR unmatched_source_analysis.confidence "
                "      IS NOT excluded.confidence "
                "   OR unmatched_source_analysis.title IS NOT excluded.title "
                "   OR unmatched_source_analysis.artist IS NOT excluded.artist "
                "   OR unmatched_source_analysis.album IS NOT excluded.album "
                "   OR unmatched_source_analysis.duration_ms "
                "      IS NOT excluded.duration_ms "
                "   OR unmatched_source_analysis.source_path "
                "      IS NOT excluded.source_path "
                "   OR unmatched_source_analysis.modified_at "
                "      <> excluded.modified_at",
                (
                    SOURCE,
                    staged.source_row_id,
                    staged.field_name,
                    json.dumps(
                        staged.value, sort_keys=True, separators=(",", ":")
                    ),
                    staged.unmatched_reason,
                    staged.confidence,
                    staged.title,
                    staged.artist,
                    staged.album,
                    staged.duration_ms,
                    staged.source_path,
                    staged.modified_at,
                    stamp,
                ),
            )
            if cursor.rowcount:
                result.staged_written += 1
            else:
                result.staged_unchanged += 1

        conn.execute(
            "INSERT INTO events(ts, kind, stable_id, payload_json, actor) "
            "VALUES (?, ?, NULL, ?, ?)",
            (
                stamp,
                "mik.load",
                json.dumps(
                    {
                        "fields_written": result.fields_written,
                        "fields_unchanged": result.fields_unchanged,
                        "segment_tracks": result.segment_tracks,
                        "segment_rows": result.segment_rows,
                        "segment_tracks_unchanged": result.segment_tracks_unchanged,
                        "staged_written": result.staged_written,
                        "staged_unchanged": result.staged_unchanged,
                        "gate": plan.gate_summary,
                        "verification_basis": {
                            row["field_name"]: row["basis"]
                            for row in plan.verification
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                actor,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


__all__ = [
    "GATED_FIELDS",
    "KEY_CONFIDENCE_FLOOR",
    "KEY_CONFIDENCE_MIK_WINS",
    "MIK_CONFIDENCE",
    "OUTRANKS_MIK",
    "SCALAR_FIELDS",
    "SEGMENTS_FIELD",
    "ApplyResult",
    "FieldWrite",
    "LoadError",
    "LoadPlan",
    "SegmentWrite",
    "StagedWrite",
    "apply_plan",
    "build_plan",
]
