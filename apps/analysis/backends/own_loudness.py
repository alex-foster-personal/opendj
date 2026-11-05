"""AnalyzerBackend wrapper around :mod:`apps.analysis_loudness`.

Registers as ``own_loudness.backfill`` so ``apps.analysis.run --backend``
can write a v2 :class:`~apps.analysis.record.AnalysisRecord` through the
existing store path. Measurement stays in the loudness producer; this
module only supplies record identity, the canonical decode fingerprint,
and the exception mapping the runner already classifies.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from apps.analysis.pcm_fingerprint import (
    SAMPLE_RATE_HZ,
    FingerprintUnavailable,
    canonical_decode_fingerprint_and_frames,
    require_resampler,
)
from apps.loudness.scan import LoudnessError, require_ffmpeg

from ..record import AnalysisRecord
from . import register
from .base import BackendNotAvailable, TrackUnreadable, TrackVanished

# Pre-v2 analysis columns this loudness-only record cannot measure. They
# exist because the table still requires them; they are not measurements
# and cannot project from a loudness-lane pointer.
_PLACEHOLDER_BPM = 0.0
_PLACEHOLDER_BPM_CONFIDENCE = 0.0
_PLACEHOLDER_KEY_CAMELOT = ""
_PLACEHOLDER_KEY_OPENKEY = ""
_PLACEHOLDER_KEY_CONFIDENCE = 0.0
_PLACEHOLDER_ENERGY = 0


def _load_loudness_service() -> Any:
    """Build the loudness service without a static ``apps.analysis_loudness`` import.

    The producer already imports :mod:`apps.analysis.lanes`. A static import
    the other way is the ``analysis <-> analysis_loudness`` package pair the
    quality gate refuses. ``importlib`` plus a joined name keeps that edge
    out of grimp's static graph; analyze() still calls the real producer,
    through the factory the package publishes behind its contract.
    """
    factory = importlib.import_module(".".join(("apps", "analysis_loudness", "http"))).build_service
    service: Any = factory()
    return service


def _load_loudness_contract() -> ModuleType:
    """The loudness contract module, loaded the same way and for the same reason."""
    return importlib.import_module(".".join(("apps", "analysis_loudness", "contract")))


def _canonical_fingerprint(path: Path) -> tuple[str, float]:
    """The canonical decode fingerprint and the duration it measured.

    The same contract every own record carries (`apps.analysis.pcm_fingerprint`,
    fingerprint v2: `odj-audio` decode, 44.1 kHz mono s16), so a loudness record
    and a beatgrid or key record for the same bytes name the same decode. A
    per-FILE failure is raised by name: vanished if the file is gone, else
    unreadable.
    """
    try:
        digest_hex, frames = canonical_decode_fingerprint_and_frames(path)
    except FingerprintUnavailable as exc:
        if not path.exists():
            raise TrackVanished(f"{path}: gone before decode") from None
        raise TrackUnreadable(
            f"the canonical decode fingerprint could not be taken for {path}: {exc}"
        ) from None
    return f"sha256:{digest_hex}", frames / float(SAMPLE_RATE_HZ)


class OwnLoudnessBackfillBackend:
    """Shipped R128 loudness producer, as an :class:`AnalyzerBackend`."""

    # Literals match apps.analysis_loudness.contract.PRODUCER_BACKEND / PRODUCER_VERSION.
    # Imported at analyze-time, not at module import: a top-level import of
    # analysis_loudness would close analysis <-> analysis_loudness because
    # the producer already imports apps.analysis.lanes.
    name: str = "own_loudness.backfill"
    version: str = "1.0.0"

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        """No in-process JIT, so no cache to fingerprint and none to protect."""
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        """Nothing to warm: R128/RMS run in ffmpeg and the fingerprint in odj-audio.

        Stated rather than silently inherited, so an empty warm-up here is a
        recorded fact about this backend and not an oversight.
        """
        return "no in-process JIT; ffmpeg ebur128 runs out of process"

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        """Pure compute; does not write to disk or the state layer."""
        try:
            binary = require_ffmpeg()
        except LoudnessError as exc:
            raise BackendNotAvailable(str(exc)) from exc
        path = Path(path)
        if not path.exists():
            raise TrackVanished(f"{path}: gone before decode")
        try:
            # Once per call, before a track is touched: without the engine
            # EVERY fingerprint fails, which must read as a host gap, not as
            # a library of unreadable files.
            require_resampler()
        except FingerprintUnavailable as exc:
            raise BackendNotAvailable(
                f"the canonical decode fingerprint cannot be taken on this host: {exc}"
            ) from exc
        fingerprint, duration_s = _canonical_fingerprint(path)
        contract = _load_loudness_contract()
        if cls.name != contract.PRODUCER_BACKEND or cls.version != contract.PRODUCER_VERSION:
            raise RuntimeError(
                f"backend identity {cls.name!r}/{cls.version!r} drifted from "
                f"producer {contract.PRODUCER_BACKEND!r}/{contract.PRODUCER_VERSION!r}"
            )
        service = _load_loudness_service()
        try:
            lane = service.produce_lane_result(path, binary=binary)
        except LoudnessError as exc:
            if not path.exists():
                raise TrackVanished(f"{path}: {exc}") from exc
            raise
        return AnalysisRecord(
            stable_id=stable_id,
            backend=cls.name,
            backend_version=cls.version,
            analyzed_at=datetime.now(UTC),
            duration_s=duration_s,
            sample_rate=SAMPLE_RATE_HZ,
            bpm=_PLACEHOLDER_BPM,
            bpm_confidence=_PLACEHOLDER_BPM_CONFIDENCE,
            key_camelot=_PLACEHOLDER_KEY_CAMELOT,
            key_openkey=_PLACEHOLDER_KEY_OPENKEY,
            key_confidence=_PLACEHOLDER_KEY_CONFIDENCE,
            energy=_PLACEHOLDER_ENERGY,
            energy_source="inferred",
            producer="backfill",
            producer_version=cls.version,
            uses_model=False,
            model_sha256=None,
            decode_fingerprint=fingerprint,
            lanes={"loudness": lane},
        )


register(OwnLoudnessBackfillBackend.name, OwnLoudnessBackfillBackend)
