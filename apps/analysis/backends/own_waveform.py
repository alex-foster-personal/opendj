"""The own waveform BACKFILL producer, as an `apps.analysis` backend.

`python -m apps.analysis.run --backend own_waveform.backfill --files track.wav`
writes an `AnalysisRecord` v2 row under backend ``own_waveform.backfill``,
carrying the `waveform` lane block that `/anlz` then serves when the lane's
effective source is own. The registry key and the stored backend string are the
same string for the reason `apps/analysis/backends/own_beatgrid.py` spells out:
`run.py` filters already-analyzed tracks by the CLI's own `--backend` value, so
an alias would silently make `--only-missing` match nothing.

Model-free: no weights of any kind, so every record carries `uses_model=False`
and `model_sha256=None`. Pre-v2 columns (`bpm`, `key_*`, `energy`) are the "not
measured" sentinels own_key uses; canonical readers consult the lane block.

When PCM cannot be produced for fingerprinting, the digest is sha256 over the
**source file bytes**. That identifies the bytes we could not decode, so a
second run of the same garbage file is semantically equal (idempotent) and a
repaired file is not. The lane is `failed`, so no consumer may treat this
digest as a player-PCM identity.

-Cursor
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from apps.analysis.pcm_fingerprint import (
    FingerprintUnavailable,
    canonical_decode_fingerprint,
    require_resampler,
)
from apps.analysis_waveform import decode
from apps.analysis_waveform.decode import (
    DETAIL_COLUMNS_PER_S,
    LocalDecodeUnavailable,
)
from apps.analysis_waveform.lane_payload import (
    REASON_NOT_DECODED,
    build_waveform_payload,
)
from apps.analysis_waveform.version import LANE, PRODUCER, PRODUCER_VERSION

from ..lanes import LaneResult, own_backend
from ..record import AnalysisRecord
from . import register
from .base import BackendNotAvailable, TrackVanished

log = logging.getLogger("apps.analysis.backends.own_waveform")

BACKEND_NAME = own_backend(LANE, PRODUCER)
OWN_WAVEFORM_BACKEND = BACKEND_NAME


#-----------------------------------------------------------------------------
# payload -> record
#-----------------------------------------------------------------------------

def _stamp_fingerprint(raw: str) -> str:
    return raw if raw.startswith("sha256:") else f"sha256:{raw}"


def _file_bytes_fingerprint(audio_path: Path) -> str:
    digest = hashlib.sha256()
    with audio_path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def record_from_peaks(
    stable_id: str,
    peaks: np.ndarray,
    *,
    duration_s: float,
    sample_rate: int,
    decode_fingerprint: str,
) -> AnalysisRecord:
    """An ok record from decoded peak columns."""
    return AnalysisRecord(
        stable_id=stable_id,
        backend=BACKEND_NAME,
        backend_version=PRODUCER_VERSION,
        analyzed_at=datetime.now(UTC),
        duration_s=duration_s,
        sample_rate=sample_rate,
        bpm=0.0,
        bpm_confidence=0.0,
        key_camelot="",
        key_openkey="",
        key_confidence=0.0,
        energy=0,
        energy_source="inferred",
        producer=PRODUCER,
        producer_version=PRODUCER_VERSION,
        uses_model=False,
        model_sha256=None,
        decode_fingerprint=_stamp_fingerprint(decode_fingerprint),
        lanes={
            LANE: LaneResult(
                status="ok",
                payload=build_waveform_payload(peaks),
            )
        },
    )


def record_from_decode_error(
    stable_id: str,
    reason_detail: str,
    decode_fingerprint: str,
) -> AnalysisRecord:
    """A failed record when decode could not produce peaks."""
    return AnalysisRecord(
        stable_id=stable_id,
        backend=BACKEND_NAME,
        backend_version=PRODUCER_VERSION,
        analyzed_at=datetime.now(UTC),
        duration_s=0.0,
        sample_rate=0,
        bpm=0.0,
        bpm_confidence=0.0,
        key_camelot="",
        key_openkey="",
        key_confidence=0.0,
        energy=0,
        energy_source="inferred",
        producer=PRODUCER,
        producer_version=PRODUCER_VERSION,
        uses_model=False,
        model_sha256=None,
        decode_fingerprint=_stamp_fingerprint(decode_fingerprint),
        lanes={
            LANE: LaneResult(
                status="failed",
                reason=f"{REASON_NOT_DECODED}: {reason_detail}",
                payload={},
            )
        },
    )


#-----------------------------------------------------------------------------
# backend
#-----------------------------------------------------------------------------

def _resolve_decode_fingerprint(audio_path: Path) -> tuple[str, bool]:
    """Canonical PCM fingerprint, or source-byte hash when PCM cannot be produced.

    Returns ``(digest_hex, pcm_fingerprint)`` so a successful decode can
    re-check the PCM identity only when one was actually taken.
    """
    try:
        return canonical_decode_fingerprint(audio_path), True
    except FingerprintUnavailable:
        if not audio_path.exists():
            raise TrackVanished(
                f"{audio_path} vanished while its fingerprint was being taken"
            ) from None
        return _file_bytes_fingerprint(audio_path), False


def analyze_audio(audio_path: Path, stable_id: str) -> AnalysisRecord:
    """Decode peaks and build the v2 record for one track."""
    require_resampler()
    decode_fingerprint, pcm_fingerprint = _resolve_decode_fingerprint(audio_path)
    try:
        peaks, sample_rate = decode.decode_peaks_measured(
            decode.select_decoder(audio_path), audio_path
        )
    except LocalDecodeUnavailable as exc:
        if exc.retryable:
            raise BackendNotAvailable(str(exc)) from exc
        return record_from_decode_error(stable_id, exc.reason, decode_fingerprint)

    if pcm_fingerprint and canonical_decode_fingerprint(audio_path) != decode_fingerprint:
        raise TrackVanished(
            f"{audio_path} changed while waveform analysis was running, so its "
            "peaks came from bytes this record cannot vouch for"
        )

    duration_s = float(peaks.shape[0]) / DETAIL_COLUMNS_PER_S
    return record_from_peaks(
        stable_id,
        peaks,
        duration_s=duration_s,
        sample_rate=sample_rate,
        decode_fingerprint=decode_fingerprint,
    )


class OwnWaveformBackfillBackend:
    """`apps.analysis.backends.base.AnalyzerBackend` for the own waveform lane."""

    name: str = BACKEND_NAME
    version: str = PRODUCER_VERSION

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        audio_path = Path(path)
        if not audio_path.exists():
            raise TrackVanished(f"{audio_path} was gone before the decode opened it")
        try:
            return analyze_audio(audio_path, stable_id)
        except FingerprintUnavailable as exc:
            if not audio_path.exists():
                raise TrackVanished(
                    f"{audio_path} vanished while its fingerprint was being taken"
                ) from None
            raise BackendNotAvailable(
                f"the canonical decode fingerprint cannot be taken on this host: {exc}"
            ) from exc

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        return f"{BACKEND_NAME}: no JIT cache to warm; waveform decode uses ffmpeg"


register(OwnWaveformBackfillBackend.name, OwnWaveformBackfillBackend)

__all__ = [
    "BACKEND_NAME",
    "OWN_WAVEFORM_BACKEND",
    "OwnWaveformBackfillBackend",
    "analyze_audio",
    "record_from_decode_error",
    "record_from_peaks",
]
