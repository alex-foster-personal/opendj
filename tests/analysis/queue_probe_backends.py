"""Backends the queue tests drive, including one that kills its own worker.

A separate importable module, like :mod:`tests.analysis.pool_probe_backends`,
because the queue runner hands SPAWNED workers the backend CLASS: the child
imports this module by reference when it unpickles the work item, so it must
be importable from the repository root rather than defined inside a test
function.

Every backend here writes a marker file per analyzed track into
``MDT_QUEUE_PROBE_DIR`` before returning. That directory is how the
process-restart test counts REAL executions across two processes, which is
the only way to tell "resumed exactly once" from "re-run and overwritten
idempotently": the record row alone cannot, because an idempotent second
write looks identical to no second write.

-Claude
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from apps.analysis.depends_on import DEPENDS_ON_KEY, dependency_identity
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord

PROBE_DIR_ENV: str = "MDT_QUEUE_PROBE_DIR"
PROBE_DB_ENV: str = "MDT_QUEUE_PROBE_DB"
PROBE_SLEEP_ENV: str = "MDT_QUEUE_PROBE_SLEEP_S"

_FAKE_DECODE_FP = "sha256:" + "ab" * 32


def _mark(stable_id: str, backend: str) -> None:
    out = Path(os.environ[PROBE_DIR_ENV]) / f"{uuid.uuid4().hex}.json"
    out.write_text(
        json.dumps({"stable_id": stable_id, "backend": backend, "pid": os.getpid()}),
        encoding="utf-8",
    )


def _sleep_if_asked() -> None:
    seconds = float(os.environ.get(PROBE_SLEEP_ENV, "0") or "0")
    if seconds > 0:
        time.sleep(seconds)


def _beatgrid_payload() -> dict[str, object]:
    return {
        "beats": [
            {"n": (i % 4) + 1, "bpm": 128.0, "t": round(i * 0.46875, 5)}
            for i in range(8)
        ],
        "bpm": 128.0,
        "bpm_confidence": 0.9,
        "octave_reason": "probe: fixed",
        "first_downbeat_s": 0.0,
        "tempo_changes": [],
        "static_grid_untrusted": False,
    }


def _key_payload() -> dict[str, object]:
    return {
        "camelot": "8A",
        "openkey": "1m",
        "pitch_class": 9,
        "is_minor": True,
        "confidence": 0.8,
        "segments": {"status": "missing", "reason": "probe", "segments": []},
    }


def _base_record(
    stable_id: str, backend: str, version: str, lane: str, result: LaneResult,
    features: dict[str, object] | None = None,
) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=stable_id,
        backend=backend,
        backend_version=version,
        analyzed_at=datetime.now(UTC),
        duration_s=180.0,
        sample_rate=44100,
        bpm=128.0,
        bpm_confidence=0.9,
        key_camelot="8A",
        key_openkey="1m",
        key_confidence=0.8,
        energy=5,
        energy_source="inferred",
        producer="backfill",
        producer_version=version,
        uses_model=False,
        model_sha256=None,
        decode_fingerprint=_FAKE_DECODE_FP,
        lanes={lane: result},
        features_blob=features or {},
    )


class BeatgridProbeV1:
    """A beatgrid producer at 1.0.0 that always succeeds."""

    name = "own_beatgrid.backfill"
    version = "1.0.0"

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        return "queue probe backend has no JIT cache"

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        _mark(stable_id, cls.name)
        _sleep_if_asked()
        return _base_record(
            stable_id,
            cls.name,
            cls.version,
            "beatgrid",
            LaneResult(status="ok", confidence=0.9, payload=_beatgrid_payload()),
        )


class BeatgridProbeV2(BeatgridProbeV1):
    """The same producer after a version bump. Different grid content."""

    version = "2.0.0"

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        _mark(stable_id, f"{cls.name}@{cls.version}")
        _sleep_if_asked()
        payload = _beatgrid_payload()
        payload["octave_reason"] = "probe: v2 re-fit"
        return _base_record(
            stable_id,
            cls.name,
            cls.version,
            "beatgrid",
            LaneResult(status="ok", confidence=0.95, payload=payload),
        )


class KeyProbeV1:
    """A key producer that declares what beatgrid it was computed against.

    Reads the canonical beatgrid from the state DB named by
    :data:`PROBE_DB_ENV`, exactly as a real key producer has to (its
    bar-synchronous chroma needs own downbeats). With no canonical beatgrid
    it records ``missing`` with reason ``no_own_downbeats`` and NO
    ``depends_on`` identity, which is the state the queue's static lane edge
    exists to rescue.
    """

    name = "own_key.backfill"
    version = "1.0.0"

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        return "queue probe backend has no JIT cache"

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        _mark(stable_id, cls.name)
        _sleep_if_asked()
        conn = sqlite3.connect(os.environ[PROBE_DB_ENV])
        try:
            row = conn.execute(
                "SELECT backend, backend_version FROM analysis_canonical "
                "WHERE stable_id = ? AND lane = 'beatgrid'",
                (stable_id,),
            ).fetchone()
            if row is None:
                return _base_record(
                    stable_id,
                    cls.name,
                    cls.version,
                    "key",
                    LaneResult(status="missing", reason="no_own_downbeats"),
                )
            record_json = conn.execute(
                "SELECT record_json FROM analysis WHERE stable_id = ? AND "
                "backend = ? AND backend_version = ?",
                (stable_id, row[0], row[1]),
            ).fetchone()[0]
        finally:
            conn.close()
        beatgrid = AnalysisRecord.from_json(record_json)
        return _base_record(
            stable_id,
            cls.name,
            cls.version,
            "key",
            LaneResult(status="ok", confidence=0.8, payload=_key_payload()),
            features={
                DEPENDS_ON_KEY: {"beatgrid": dependency_identity(beatgrid)}
            },
        )


__all__ = [
    "PROBE_DB_ENV",
    "PROBE_DIR_ENV",
    "PROBE_SLEEP_ENV",
    "BeatgridProbeV1",
    "BeatgridProbeV2",
    "KeyProbeV1",
]
