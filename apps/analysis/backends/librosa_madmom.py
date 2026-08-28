"""Explicit learned beat/downbeat backend using librosa and madmom.

librosa code is ISC. madmom code is BSD-3-Clause, while its bundled
pre-trained models are CC-BY-NC-SA 4.0 and restricted to non-commercial
use. madmom itself is a git-HEAD install (requirements.txt,
``--no-build-isolation``) that no pyproject extra can supply, so this
backend is development-only, must be selected explicitly, and is never
an automatic fallback for the portable ``librosa`` default.

* BPM + beats: ``madmom.features.beats.{RNN,DBN}BeatTrackingProcessor``.
* Downbeats:   ``madmom.features.downbeats.DBNDownBeatTrackingProcessor``.
* Everything else (onsets, RMS, key, energy): inherited from
  :class:`~apps.analysis.backends.librosa.LibrosaBackend`.
"""
from __future__ import annotations

import logging

import numpy as np

from . import register
from .base import BackendNotAvailable
from .librosa import LibrosaBackend, _bpm_from_beats

log = logging.getLogger(__name__)


class LibrosaMadmomBackend(LibrosaBackend):
    """Librosa feature pipeline with madmom learned beat and downbeat models."""

    name: str = "librosa+madmom"
    version: str = ""
    beat_tracking: str = "madmom.features.beats.RNNBeatProcessor"
    downbeat_tracking: bool = True

    @classmethod
    def _require_deps(cls) -> None:
        super()._require_deps()
        try:
            import madmom  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise BackendNotAvailable(
                "librosa+madmom backend requires the dev-only madmom git-HEAD "
                "install (see requirements.txt); its pre-trained models are "
                "CC-BY-NC-SA 4.0 and restricted to non-commercial use"
            ) from exc

    @classmethod
    def _version(cls) -> str:
        if cls.version:
            return cls.version
        import librosa
        import madmom
        import scipy

        cls.version = (
            f"librosa=={librosa.__version__}"
            f"+madmom=={madmom.__version__}"
            f"+numpy=={np.__version__}"
            f"+scipy=={scipy.__version__}"
        )
        return cls.version

    @classmethod
    def _beats_and_bpm(
        cls, y: np.ndarray, sr: int
    ) -> tuple[float, float, list[float], list[float]]:
        from madmom.features.beats import DBNBeatTrackingProcessor, RNNBeatProcessor
        from madmom.features.downbeats import (
            DBNDownBeatTrackingProcessor,
            RNNDownBeatProcessor,
        )
        try:
            rnn_beat = RNNBeatProcessor()(y.astype(np.float32))
            beats = DBNBeatTrackingProcessor(fps=100)(rnn_beat)
            beats_s = [float(b) for b in beats.tolist()]
        except Exception as exc:  # pragma: no cover
            log.warning("madmom beat tracking failed: %s", exc)
            beats_s = []
        bpm, bpm_conf = _bpm_from_beats(beats_s)

        try:
            rnn_db = RNNDownBeatProcessor()(y.astype(np.float32))
            dbn = DBNDownBeatTrackingProcessor(beats_per_bar=[3, 4], fps=100)
            dbs = dbn(rnn_db)
            downbeats_s = [float(row[0]) for row in dbs if int(row[1]) == 1]
        except Exception as exc:  # pragma: no cover
            log.warning("madmom downbeat tracking failed: %s", exc)
            downbeats_s = []

        return bpm, bpm_conf, beats_s, downbeats_s


register(LibrosaMadmomBackend.name, LibrosaMadmomBackend)

__all__ = ["LibrosaMadmomBackend"]
