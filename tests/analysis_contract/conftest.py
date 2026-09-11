"""Fixtures for the native-analysis v1 contract tests.

Why this directory is not ``tests/analysis``: that package's ``conftest.py``
does ``pytest.importorskip("soundfile")`` at MODULE level, so a venv without
the analysis extra skips the entire directory and pytest still exits 0 with
"no tests ran". The fast CI lane also passes ``--ignore=tests/analysis``.
Either would turn every test below into a check that cannot fail, which is
exactly what `.claude/rules/verification.md` forbids. These tests need no
audio stack at all -- they are pure dataclass, SQL and selection logic -- so
they live where they actually run.

-Claude
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from apps.analysis import store as store_mod
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord

STAMP = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)

# Real sha256 digests. The contract now checks the SHAPE of both digest
# fields, so a fixture using a readable placeholder would be exercising a
# record the store refuses.
DECODE_FINGERPRINT = "sha256:663de53948b9c9d36e558f3db58a301677b498eb67da9637338ea0ba296965e8"
MODEL_SHA256 = "sha256:227e7b8b1facb2a9684479407fb7869ae9500a22f70f0af9d43d6984edf96ebb"


# `own_record`'s duration_s is 300.0. The default grid below ends EXACTLY
# there -- not past it -- because `beats_within_duration` (Codex P2, PR
# #1562, round two: the first fix's own grid ran to 310.312s against a
# 300s record) now refuses a grid that outlives the record. Ending exactly
# on the boundary, rather than short of it, keeps a tempo change legally
# placeable at the boundary itself (`at_s == duration_s`).
_FIXTURE_GRID_SPAN_S = 300.0

# Stand-in beatgrid dependency for key payloads: contract tests validate shape,
# not staleness against a live canonical beatgrid row.
_DEPENDS_ON_BEATGRID = {
    "backend": "own_beatgrid.inapp",
    "producer_version": "1.0.0",
    "model_sha256": MODEL_SHA256,
    "decode_fingerprint": DECODE_FINGERPRINT,
    "record_digest": "sha256:" + "2f" * 32,
}


def beatgrid_payload(
    bpm: float = 128.0, tempo_changes: int = 0, grid_span_s: float = _FIXTURE_GRID_SPAN_S,
) -> dict[str, Any]:
    """A beatgrid payload with a real 1-2-3-4 cadence spanning ``[0, grid_span_s]``.

    The next-to-last beat falls out of the regular `interval` cadence; the
    LAST beat is then pinned to exactly ``grid_span_s`` regardless of where
    that cadence lands, so the grid never exceeds it (whatever `bpm` is) and
    a caller can still place a marker on the boundary itself. Pass a
    `grid_span_s` past `own_record`'s 300.0 to build a grid that
    deliberately outlives the record, for a test of `beats_within_duration`
    itself.
    """
    interval = 60.0 / bpm
    beats: list[dict[str, Any]] = []
    n = 1
    i = 0
    while i * interval < grid_span_s:
        beats.append({"t": round(i * interval, 3), "n": n, "bpm": bpm})
        n = 1 if n == 4 else n + 1
        i += 1
    if beats[-1]["t"] < grid_span_s:
        # Rounding the regular cadence to 3 decimals can itself land exactly
        # on grid_span_s (e.g. 299.9996 rounds to 300.0); only append the
        # boundary beat when that has not already happened, or the two
        # would tie and fail the strictly-increasing check below.
        beats.append({"t": grid_span_s, "n": n, "bpm": bpm})
    # A tempo change is only ever a real one when it sits AT a beat (P2,
    # PR #1562): the producer, apps.analysis_beatgrid.tempo_change
    # .detect_tempo_changes, emits every marker at `beats[split]`. Spreading
    # `tempo_changes` interior beats evenly across the grid (never the first
    # or last -- the boundary tests exercise those directly) keeps this
    # fixture a payload the contract actually accepts.
    marker_indices = [
        round((i + 1) * (len(beats) - 1) / (tempo_changes + 1))
        for i in range(tempo_changes)
    ]
    return {
        "beats": beats,
        "bpm": bpm,
        "bpm_confidence": 0.93,
        "octave_reason": "beat interval admits only one octave in 60..200",
        "first_downbeat_s": 0.0,
        "tempo_changes": [
            {"at_s": beats[idx]["t"], "bpm_before": bpm, "bpm_after": bpm * 1.03,
             "confidence": 0.8}
            for idx in marker_indices
        ],
        "static_grid_untrusted": tempo_changes > 0,
    }


def key_payload(camelot: str = "8A", segments: int = 1) -> dict[str, Any]:
    """A key payload that is INTERNALLY CONSISTENT for whatever key is asked.

    `pitch_class` and `is_minor` are derived from `camelot` rather than
    hardcoded: the contract cross-checks that the three spellings of one
    measurement agree, so a fixture pinning `pitch_class: 9` beside an
    arbitrary key would be testing a record the store refuses.
    """
    from apps.analysis.lane_payloads import _CAMELOT_PITCH_CLASS

    pitch_class, is_minor = _CAMELOT_PITCH_CLASS[camelot]
    return {
        "camelot": camelot,
        "openkey": "1m",
        "pitch_class": pitch_class,
        "is_minor": is_minor,
        "confidence": 0.77,
        "segments": {
            "status": "ok",
            "reason": None,
            "segments": [
                {
                    "start_bar": i * 16, "end_bar": (i + 1) * 16,
                    "start_s": i * 30.0, "end_s": (i + 1) * 30.0,
                    "key_camelot": camelot, "key_openkey": "1m",
                    "confidence": 0.7,
                }
                for i in range(segments)
            ],
        },
        "depends_on": {"beatgrid": dict(_DEPENDS_ON_BEATGRID)},
    }


def loudness_payload(lufs: float = -8.2, dbtp: float = -0.3) -> dict[str, Any]:
    return {
        "integrated_lufs": lufs,
        "true_peak_dbtp": dbtp,
        "loudness_range_lu": 5.1,
        "rms_db": -11.4,
    }


def waveform_payload() -> dict[str, Any]:
    return {
        "kind": "tri",
        "preview": {"length": 3, "low": [1, 2, 3], "mid": [1, 2, 3], "high": [1, 2, 3]},
        "detail": {"length": 2, "low": [4, 5], "mid": [4, 5], "high": [4, 5]},
    }


def own_record(
    *,
    stable_id: str = "t1",
    lane: str = "beatgrid",
    producer: str = "inapp",
    version: str = "1.0.0",
    result: LaneResult | None = None,
    uses_model: bool = False,
    model_sha256: str | None = None,
    decode_fingerprint: str = DECODE_FINGERPRINT,
    backend: str | None = None,
) -> AnalysisRecord:
    """A minimal, contract-valid own record. Every field is stated, not defaulted."""
    if result is None:
        payloads: dict[str, Callable[[], dict[str, Any]]] = {
            "beatgrid": beatgrid_payload,
            "key": key_payload,
            "loudness": loudness_payload,
            "waveform": waveform_payload,
            "vocal": dict,
        }
        result = LaneResult(status="ok", payload=payloads[lane]())
    if backend is None:
        backend = f"own_{lane}.{producer}"
    return AnalysisRecord(
        stable_id=stable_id,
        backend=backend,
        backend_version=version,
        analyzed_at=STAMP,
        duration_s=300.0,
        sample_rate=44100,
        bpm=128.0,
        bpm_confidence=0.93,
        key_camelot="8A",
        key_openkey="1m",
        key_confidence=0.77,
        energy=6,
        energy_source="inferred",
        producer=producer,  # type: ignore[arg-type]
        producer_version=version,
        uses_model=uses_model,
        model_sha256=model_sha256,
        decode_fingerprint=decode_fingerprint,
        lanes={lane: result},
    )


@pytest.fixture()
def db(tmp_path) -> Iterator[sqlite3.Connection]:
    """A real state.db with the Phase 5 + analysis schemas applied."""
    conn = store_mod.open_conn(tmp_path / "state.db")
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_toggles():
    """Every test starts at the launch state, and leaves it there."""
    from apps.analysis import selection

    selection.reset_toggles()
    yield
    selection.reset_toggles()
