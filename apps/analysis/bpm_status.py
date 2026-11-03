"""Named reasons for a tempo the analyzer could not measure (issue #5986).

The portable default backend (`librosa`) reports ``bpm`` as a NOT NULL REAL,
so a track whose beat tracker found fewer than two beats used to be stored,
evented and printed as BPM 0. Zero is not a tempo, and a new user must never
be shown one. This module names WHY there is no tempo instead:

* ``too_short`` - the decoded audio is shorter than librosa's tempo
  estimator window (:data:`TEMPO_WINDOW_S`), so the estimator never saw a
  full window to autocorrelate over. The short conformance fixture
  ``tests/fixtures/conformance/03-8-hot-cues/audio/cues.mp3`` (3.0 s) is the
  measured case.
* ``no_pulse`` - the audio is long enough and still carries no trackable beat
  (a sustained tone, a drone, silence).

The ``bpm`` column keeps its 0.0 storage sentinel (changing a NOT NULL column
is a schema migration, out of scope here); every reader that shows a tempo
asks :func:`bpm_status_for` first and shows the named status instead.

The listing read (:func:`unmeasured_bpm_status`) is a projection over the
``analysis`` table, scoped to rows whose ``bpm`` is not positive, so it never
substitutes a legacy value for a real one.
"""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any, Literal

#: librosa's ``feature.tempo`` default ``ac_size``: the autocorrelation window,
#: in seconds, that ``beat.beat_track`` estimates its tempo over.
TEMPO_WINDOW_S: float = 8.0

BPM_STATUS_OK = "ok"
BPM_STATUS_TOO_SHORT = "too_short"
BPM_STATUS_NO_PULSE = "no_pulse"

UnmeasuredBpmStatus = Literal["too_short", "no_pulse"]
UNMEASURED_BPM_STATUSES: tuple[UnmeasuredBpmStatus, ...] = (
    BPM_STATUS_TOO_SHORT,
    BPM_STATUS_NO_PULSE,
)

#: The analyzer whose rows this module projects for the listing.
LEGACY_TEMPO_BACKEND = "librosa"


def classify_unmeasured_tempo(duration_s: float) -> tuple[UnmeasuredBpmStatus, str]:
    """Why a track of ``duration_s`` seconds produced no tempo: (status, reason)."""
    if duration_s < TEMPO_WINDOW_S:
        return (
            BPM_STATUS_TOO_SHORT,
            f"too short for a tempo: {duration_s:.1f} s of audio, the tempo "
            f"estimator needs at least {TEMPO_WINDOW_S:g} s",
        )
    return (
        BPM_STATUS_NO_PULSE,
        f"no beat found in {duration_s:.1f} s of audio, so no tempo was measured",
    )


def bpm_status_for(
    bpm: float, duration_s: float, features_blob: Mapping[str, Any] | None = None
) -> tuple[str, str | None]:
    """(status, reason) for one stored record's tempo.

    A positive ``bpm`` is ``ok``. Otherwise the record's own stored status
    wins; a row written before this module existed (bpm 0, no status) is
    classified from its duration, so no reader ever has to show its 0.
    """
    if bpm > 0:
        return BPM_STATUS_OK, None
    blob = features_blob or {}
    stored = blob.get("bpm_status")
    reason = blob.get("bpm_reason")
    if stored in UNMEASURED_BPM_STATUSES and isinstance(reason, str) and reason:
        return str(stored), reason
    return classify_unmeasured_tempo(duration_s)


def unmeasured_bpm_status(
    conn: sqlite3.Connection, stable_ids: Iterable[str]
) -> dict[str, tuple[str, str]]:
    """``{stable_id: (status, reason)}`` for tracks whose latest legacy tempo is unmeasured.

    Only tracks whose newest ``librosa`` row has ``bpm <= 0`` appear; a
    track with a positive tempo, or with no such row, is absent. Returns
    ``{}`` when the ``analysis`` table has never been created.
    """
    ids = list(dict.fromkeys(stable_ids))
    if not ids:
        return {}
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis'"
    ).fetchone()
    if exists is None:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for i in range(0, len(ids), 500):
        sub = ids[i:i + 500]
        placeholders = ",".join("?" * len(sub))
        rows = conn.execute(
            "SELECT stable_id, bpm, duration_s, "
            "       json_extract(record_json, '$.features_blob.bpm_status'), "
            "       json_extract(record_json, '$.features_blob.bpm_reason') "
            "FROM analysis AS a "
            f"WHERE a.backend = ? AND a.stable_id IN ({placeholders}) AND a.bpm <= 0 "
            "AND a.analyzed_at = ("
            "  SELECT MAX(b.analyzed_at) FROM analysis AS b "
            "  WHERE b.stable_id = a.stable_id AND b.backend = a.backend)",
            (LEGACY_TEMPO_BACKEND, *sub),
        )
        for sid, bpm, duration_s, stored, reason in rows:
            if bpm is None or float(bpm) > 0:
                continue
            status, why = bpm_status_for(
                float(bpm),
                _stored_duration_s(sid, duration_s),
                {"bpm_status": stored, "bpm_reason": reason},
            )
            if not why:
                raise ValueError(f"no reason named for the unmeasured tempo of {sid!r}")
            out[sid] = (status, why)
    return out



def _stored_duration_s(stable_id: str, duration_s: object) -> float:
    """A row's ``duration_s`` as stored, refused when it cannot be a duration.

    The column is ``REAL NOT NULL``, so a NULL, negative or non-finite value is
    a corrupt row. Classifying it as 0 s would invent a ``too_short`` and a
    "0.0 s" reason, so it fails here, naming the row, instead.
    """
    if duration_s is None or isinstance(duration_s, bool):
        raise ValueError(f"analysis row for {stable_id!r} has no duration_s; refusing to name its tempo status")
    try:
        value = float(duration_s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"analysis row for {stable_id!r} has a non-numeric duration_s {duration_s!r}") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"analysis row for {stable_id!r} has an invalid duration_s {duration_s!r}")
    return value

__all__ = [
    "BPM_STATUS_NO_PULSE",
    "BPM_STATUS_OK",
    "BPM_STATUS_TOO_SHORT",
    "LEGACY_TEMPO_BACKEND",
    "TEMPO_WINDOW_S",
    "UNMEASURED_BPM_STATUSES",
    "UnmeasuredBpmStatus",
    "bpm_status_for",
    "classify_unmeasured_tempo",
    "unmeasured_bpm_status",
]
