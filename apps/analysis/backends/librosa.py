"""Portable default analyzer built from the permissive librosa stack.

librosa (ISC) + scipy (BSD-3-Clause) install from PyPI wheels via the
``analysis`` extra, so this backend works on a clean machine with no
git-HEAD builds. It intentionally provides beat, onset, key, RMS, and
energy analysis, and a confidence-gated bar-phase downbeat estimator.
Ambiguous or uniform material returns no downbeats. Learned madmom beat
and downbeat inference lives in the explicit ``librosa+madmom``
development backend.

* BPM + beats: ``librosa.beat.beat_track``.
* Onsets:      ``librosa.onset.onset_detect``.
* RMS peaks:   ``librosa.feature.rms`` + ``scipy.signal.find_peaks``.
* Key:         ``librosa.feature.chroma_cqt`` + Krumhansl-Schmuckler profiles.
* Energy:      RMS-dBFS binned through ``config.energy.rms_dbfs_bins``.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from .. import config as _analysis_config
from ..jit_warmup import librosa_numba_cache_roots
from ..record import AnalysisRecord
from . import register
from .base import (
    BackendNotAvailable,
    TrackTooLong,
    TrackUnreadable,
    TrackVanished,
)

log = logging.getLogger(__name__)

# Krumhansl-Schmuckler reference profiles (1982).
_KS_MAJOR = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
    dtype=np.float64,
)
_KS_MINOR = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
    dtype=np.float64,
)

# Pitch class (C=0 .. B=11) -> Camelot / open-key labels.
_CAMELOT_MAJOR = ["8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B"]
_CAMELOT_MINOR = ["5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A"]
# Open Key number = Camelot number + 5 (mod 12, 1-indexed), NOT the Camelot
# number reused directly. The previous tables here reused the Camelot number
# (a one-position rotation of the correct table, wrong for all 12 pitch
# classes, not just the A-minor case that surfaced it: A minor is pitch class
# 9, Camelot 8A, so it must canonicalize to Open Key 1m, not 8m). Source of
# truth and regression tests: apps/analysis_key/canon.py.
_OPENKEY_MAJOR = [f"{n}d" for n in [1, 8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6]]
_OPENKEY_MINOR = [f"{n}m" for n in [10, 5, 12, 7, 2, 9, 4, 11, 6, 1, 8, 3]]


def _energy_from_rms_dbfs(rms_dbfs: float, bins: list[list[float]]) -> int:
    """Bin a mean-RMS-dBFS into an integer energy 1..10."""
    for upper, level in bins:
        if rms_dbfs <= upper:
            return int(level)
    return int(bins[-1][1])


def _estimate_key(chroma: np.ndarray) -> tuple[str, str, float]:
    """Krumhansl-Schmuckler estimation on a (12, T) chroma matrix."""
    mean = chroma.mean(axis=1)
    mean = mean / (np.linalg.norm(mean) + 1e-9)
    best_corr = -2.0
    best_tonic = 0
    best_is_major = True
    for tonic in range(12):
        maj = np.roll(_KS_MAJOR, tonic) / (np.linalg.norm(_KS_MAJOR) + 1e-9)
        minr = np.roll(_KS_MINOR, tonic) / (np.linalg.norm(_KS_MINOR) + 1e-9)
        cm = float(np.dot(mean, maj))
        cn = float(np.dot(mean, minr))
        if cm > best_corr:
            best_corr = cm
            best_tonic = tonic
            best_is_major = True
        if cn > best_corr:
            best_corr = cn
            best_tonic = tonic
            best_is_major = False
    if best_is_major:
        return _CAMELOT_MAJOR[best_tonic], _OPENKEY_MAJOR[best_tonic], max(0.0, best_corr)
    return _CAMELOT_MINOR[best_tonic], _OPENKEY_MINOR[best_tonic], max(0.0, best_corr)


def _onset_strength_at_beats(
    onset_env: np.ndarray, beats_s: list[float], sr: int, hop: int
) -> list[float]:
    """Sample the onset envelope at each detected beat time."""
    import librosa

    strengths: list[float] = []
    for beat_s in beats_s:
        frame = int(librosa.time_to_frames(beat_s, sr=sr, hop_length=hop))
        frame = max(0, min(len(onset_env) - 1, frame))
        strengths.append(float(onset_env[frame]))
    return strengths


def _bar_phase_accent_scores(strengths: np.ndarray) -> list[float]:
    """Accent/other onset-strength ratio for each candidate 4/4 phase."""
    scores: list[float] = []
    for phase in range(4):
        accent = strengths[np.arange(len(strengths)) % 4 == phase]
        other = strengths[np.arange(len(strengths)) % 4 != phase]
        if accent.size < 2 or other.size < 4:
            scores.append(0.0)
            continue
        accent_mean = float(np.mean(accent))
        other_mean = float(np.mean(other))
        if other_mean <= 1e-9:
            scores.append(0.0)
            continue
        scores.append(accent_mean / other_mean)
    return scores


def _confident_bar_phase(
    scores: list[float],
    *,
    min_accent_ratio: float,
    min_phase_margin: float,
) -> int | None:
    """Return the winning 4/4 phase, or None when the accent is too weak."""
    if not scores or max(scores) < min_accent_ratio:
        return None
    ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
    best_phase, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score <= 0 or (best_score - second_score) / best_score < min_phase_margin:
        return None
    return best_phase


def estimate_downbeats_from_beats(
    beats_s: list[float],
    beat_onset_strengths: list[float],
    *,
    min_beats: int = 12,
    max_spacing_cv: float = 0.15,
    min_accent_ratio: float = 1.25,
    min_phase_margin: float = 0.15,
) -> tuple[list[float], bool]:
    """Pick a 4/4 bar phase only when accent beats are measurably stronger.

    Returns strictly increasing downbeat times and whether bar phase was
    established. Uniform or ambiguous material stays downbeat-unknown.
    """
    if len(beats_s) < min_beats or len(beat_onset_strengths) != len(beats_s):
        return [], False

    intervals = np.diff(np.asarray(beats_s))
    if intervals.size < 3:
        return [], False
    spacing_cv = float(np.std(intervals) / (np.mean(intervals) + 1e-9))
    if spacing_cv > max_spacing_cv:
        return [], False

    strengths = np.asarray(beat_onset_strengths, dtype=np.float64)
    if not np.any(strengths > 0):
        return [], False

    best_phase = _confident_bar_phase(
        _bar_phase_accent_scores(strengths),
        min_accent_ratio=min_accent_ratio,
        min_phase_margin=min_phase_margin,
    )
    if best_phase is None:
        return [], False

    downbeats = [
        float(beats_s[index])
        for index in range(len(beats_s))
        if index % 4 == best_phase
    ]
    if len(downbeats) < 3:
        return [], False
    return downbeats, True


def _bpm_from_beats(beats_s: list[float]) -> tuple[float, float]:
    """Median-interval BPM + a coefficient-of-variation confidence."""
    if len(beats_s) < 2:
        return 0.0, 0.0
    intervals = np.diff(np.asarray(beats_s))
    bpm = float(60.0 / np.median(intervals))
    coefficient_of_variation = float(np.std(intervals) / (np.mean(intervals) + 1e-9))
    confidence = max(0.0, min(1.0, 1.0 - coefficient_of_variation))
    return bpm, confidence


@lru_cache(maxsize=1)
def _file_scoped_decode_errors() -> tuple[type[BaseException], ...]:
    """Exception types measured to describe THIS FILE, not this machine.

    Measured Tue 2 Sep 2026 by calling the real ``librosa.load`` on real bad
    inputs under librosa 0.10.2 / soundfile 0.14 / audioread 3.0 with ffmpeg
    on PATH. No stubs; each line is an observed result:

      - non-audio bytes named ``.mp3``  -> audioread ``NoBackendError``
      - a zero-byte ``.wav``            -> ``EOFError``
      - a directory named ``.mp3``      -> ``IsADirectoryError``

    Two measured types are deliberately NOT in this tuple, because neither
    describes the CONTENT of the file:

      - ``FileNotFoundError`` - the path is gone. "Unreadable" and "gone" are
        different facts to the caller, so the decode site converts it to
        ``TrackVanished`` before this allow-list is consulted.
      - ``PermissionError`` - the process may not read this path. That is
        almost never about one file: a share mounted without credentials, a
        parent-directory ACL, or a macOS privacy grant that permits ``stat``
        and denies ``open`` denies EVERY track the same way, and admitting it
        here would report a whole library as individually corrupt one chunk
        at a time. It propagates, which is the systemic exit.

    Anything ABSENT here - ``ImportError``, ``MemoryError``, an ``OSError``
    that is not about this path, a bare ``RuntimeError`` out of a
    half-initialized decoder - propagates to the CLI boundary and exits
    ``EXIT_INTERNAL_ERROR``, which stops the drain rather than meeting the
    identical machine fault once per chunk.

    ``NoBackendError`` reads like a systemic "nothing is installed", and an
    earlier revision therefore admitted it only while ``available_backends``
    held more than the standard-library wav reader. That guard was wrong for
    this stack and CI proved it: the runner has no ffmpeg, so every
    undecodable file there became a systemic stop. librosa reads through
    libsndfile FIRST and only falls back to audioread once libsndfile has
    already rejected the bytes, so arriving here at all is itself evidence
    about the file. The residual is honest and stated: on a box with no
    compressed-audio decoder, each compressed track is reported individually
    rather than once - the job still ends in `error` either way.

    Imported lazily: both modules ship with librosa, so they are only
    importable once ``_require_deps`` has passed.
    """
    import audioread.exceptions  # type: ignore[import-not-found]
    import soundfile

    return (
        IsADirectoryError,
        EOFError,
        soundfile.LibsndfileError,
        audioread.exceptions.DecodeError,
    )


def _decode_failure_is_about_this_file(exc: BaseException) -> bool:
    """Split a decode failure into per-file (skip it) or systemic (stop)."""
    return isinstance(exc, _file_scoped_decode_errors())


class LibrosaBackend:
    """Portable default backend. See module docstring for feature sources."""

    name: str = "librosa"
    version: str = ""
    beat_tracking: str = "librosa.beat.beat_track"
    downbeat_tracking: bool = False

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        cls._require_deps()
        cfg = _analysis_config.load_config()
        sr = int(cfg["analyzer"]["sample_rate_hz"])
        max_minutes = float(cfg["analyzer"]["max_track_minutes"])

        import librosa

        try:
            duration_s = float(librosa.get_duration(path=str(path)))
        except Exception:
            duration_s = 0.0
        if max_minutes and duration_s and duration_s > max_minutes * 60.0:
            raise TrackTooLong(
                f"{path.name}: {duration_s:.1f}s > {max_minutes*60:.0f}s cap"
            )

        # The decode is the one step here that is about the FILE. Anything
        # that fails above it (deps, config) or below it (the beat tracker
        # operating on a valid ndarray) will fail the same way on the next
        # file, so those propagate and the caller stops.
        try:
            y, sr = librosa.load(str(path), sr=sr, mono=True)
        except FileNotFoundError as exc:
            # The caller admitted this path moments ago, so it is gone rather
            # than bad. Reported apart from TrackUnreadable because only the
            # missing-target status makes the drain retry the queue instead
            # of booking it as attempted; see TrackVanished.
            raise TrackVanished(f"{path.name}: {exc}") from exc
        except Exception as exc:
            if not _decode_failure_is_about_this_file(exc):
                raise
            raise TrackUnreadable(f"{path.name}: {exc}") from exc
        if y.size == 0:
            raise TrackUnreadable(f"empty audio: {path.name}")
        duration_s = float(len(y)) / float(sr)

        bpm, bpm_conf, beats_s, downbeats_s = cls._beats_and_bpm(y, sr)

        try:
            onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time")
            onsets_s = [float(v) for v in onsets.tolist()]
        except Exception as exc:  # pragma: no cover
            log.warning("onset_detect failed: %s", exc)
            onsets_s = []

        hop = 512
        rms = librosa.feature.rms(y=y, hop_length=hop)[0]
        rms_peaks_s = cls._rms_peaks(rms, sr=sr, hop=hop)
        mean_rms = float(np.mean(rms))
        rms_dbfs = 20.0 * np.log10(mean_rms + 1e-9)
        energy = _energy_from_rms_dbfs(rms_dbfs, cfg["energy"]["rms_dbfs_bins"])

        try:
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
            key_cam, key_ok, key_conf = _estimate_key(chroma)
        except Exception as exc:  # pragma: no cover
            log.warning("chroma_cqt/key failed: %s", exc)
            # A fixed, internally-consistent fallback pair (key_confidence=0.0
            # marks it as a non-estimate either way): _CAMELOT_MINOR[8] and
            # _OPENKEY_MINOR[8] both describe pitch class 8, unlike the old
            # literal "1A"/"1m" pair, which stopped agreeing once the Open Key
            # table above was corrected ("1A" is pitch class 8, but "1m" is
            # pitch class 9 under the corrected table).
            key_cam, key_ok, key_conf = _CAMELOT_MINOR[8], _OPENKEY_MINOR[8], 0.0

        features_blob = cls._build_features_blob(
            rms=rms,
            beats=beats_s,
            bpm=bpm,
            downbeat_tracking=bool(downbeats_s),
        )

        return AnalysisRecord(
            stable_id=stable_id,
            backend=cls.name,
            backend_version=cls._version(),
            analyzed_at=datetime.now(UTC),
            duration_s=duration_s,
            sample_rate=int(sr),
            bpm=float(bpm),
            bpm_confidence=float(bpm_conf),
            key_camelot=key_cam,
            key_openkey=key_ok,
            key_confidence=float(key_conf),
            energy=int(energy),
            energy_source="inferred",
            onsets_s=onsets_s[:5000],
            downbeats_s=downbeats_s[:2000],
            rms_peaks_s=rms_peaks_s[:2000],
            features_blob=features_blob,
        )

    #: Length of the synthetic warm-up signal. Long enough for the CQT's
    #: lowest-octave filters (~0.75 s at C1) and for the beat tracker to run
    #: its DP over a real number of frames; short enough that the warmed calls
    #: themselves are a fraction of the compile they trigger.
    _WARMUP_SECONDS: float = 3.0

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        """Where numba writes this backend's cached compilations (librosa's)."""
        return librosa_numba_cache_roots()

    @classmethod
    def warm_jit_cache(cls) -> str:
        """Drive every librosa entry point ``analyze`` uses, on 3 s of noise.

        Not a guess at which functions are numba-cached: it calls the SAME
        entry points ``analyze`` calls, so whatever they compile is what gets
        compiled here, and a librosa upgrade that moves a ``cache=True``
        decorator needs no change to this list. Beats go through
        ``cls._beats_and_bpm`` rather than ``librosa.beat.beat_track``
        directly, so a subclass that swaps the beat tracker (see
        :class:`LibrosaMadmomBackend`) warms ITS tracker and not one it never
        calls.

        The signal is ``float32`` on purpose. ``librosa.load`` returns
        ``float32``, and numba caches per type signature, so warming with the
        ``float64`` that ``np.random`` hands back by default would compile
        signatures the real run never loads and leave the real ones to race.

        Deterministic input (fixed seed), because a warm-up that behaved
        differently run to run would make a failure here unreproducible.
        """
        cls._require_deps()

        import librosa

        cfg = _analysis_config.load_config()
        sr = int(cfg["analyzer"]["sample_rate_hz"])
        rng = np.random.default_rng(1316)
        n = int(sr * cls._WARMUP_SECONDS)
        y = rng.standard_normal(n).astype(np.float32) * np.float32(0.05)
        # A click every half second, so the beat tracker has real peaks to
        # run its dynamic program over rather than an all-noise envelope.
        y[:: sr // 2] = np.float32(0.9)

        cls._beats_and_bpm(y, sr)
        librosa.onset.onset_strength(y=y, hop_length=512)
        librosa.onset.onset_detect(y=y, sr=sr, units="time")
        rms = librosa.feature.rms(y=y, hop_length=512)[0]
        cls._rms_peaks(rms, sr=sr, hop=512)
        librosa.feature.chroma_cqt(y=y, sr=sr)
        return (
            f"warmed {cls.beat_tracking}+onset_strength+onset_detect+rms+chroma_cqt on "
            f"{cls._WARMUP_SECONDS:g}s float32 @ {sr}Hz"
        )

    @classmethod
    def _require_deps(cls) -> None:
        try:
            import librosa  # noqa: F401
            import scipy  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise BackendNotAvailable(
                f"librosa backend: missing dependency {exc.name!r}; "
                "install the `analysis` extra (music-dj-tools[analysis])"
            ) from exc

    @classmethod
    def _version(cls) -> str:
        if cls.version:
            return cls.version
        import librosa
        import scipy

        cls.version = (
            f"librosa=={librosa.__version__}"
            f"+numpy=={np.__version__}"
            f"+scipy=={scipy.__version__}"
        )
        return cls.version

    @classmethod
    def _beats_and_bpm(
        cls, y: np.ndarray, sr: int
    ) -> tuple[float, float, list[float], list[float]]:
        import librosa

        hop = 512
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, sparse=True)
        beats = librosa.frames_to_time(beat_frames, sr=sr)
        beats_s = [float(v) for v in beats.tolist()]
        bpm, conf = _bpm_from_beats(beats_s)
        if bpm <= 0:
            tempo_values = np.asarray(tempo).reshape(-1)
            bpm = float(tempo_values[0]) if tempo_values.size else 0.0
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
        strengths = _onset_strength_at_beats(onset_env, beats_s, sr, hop)
        downbeats_s, _tracked = estimate_downbeats_from_beats(beats_s, strengths)
        return bpm, conf, beats_s, downbeats_s

    @classmethod
    def _rms_peaks(cls, rms: np.ndarray, sr: int, hop: int) -> list[float]:
        if rms.size < 3:
            return []
        try:
            from scipy.signal import find_peaks
            p90 = float(np.percentile(rms, 90)) - float(np.median(rms))
            p90 = max(p90, 1e-6)
            idx, _ = find_peaks(rms, prominence=p90)
        except Exception:  # pragma: no cover
            return []
        times = (np.asarray(idx) * hop) / float(sr)
        return [float(t) for t in times.tolist()]

    @classmethod
    def _build_features_blob(
        cls,
        *,
        rms: np.ndarray,
        beats: list[float],
        bpm: float,
        downbeat_tracking: bool,
    ) -> dict[str, Any]:
        # bpm_per_frame: reciprocal of local beat intervals, downsampled.
        if len(beats) >= 4:
            intervals = np.diff(np.asarray(beats))
            bpms = 60.0 / np.clip(intervals, 1e-3, None)
            if bpms.size > 256:
                idx = np.linspace(0, bpms.size - 1, 256).astype(int)
                bpms = bpms[idx]
            bpm_per_frame = [float(v) for v in bpms.tolist()]
        else:
            bpm_per_frame = []
        if rms.size > 512:
            idx = np.linspace(0, rms.size - 1, 512).astype(int)
            rms_ds = rms[idx]
        else:
            rms_ds = rms
        return {
            "beat_tracking": cls.beat_tracking,
            "downbeat_tracking": downbeat_tracking,
            "bpm_reported": float(bpm),
            "bpm_per_frame": bpm_per_frame,
            "rms": [float(v) for v in rms_ds.tolist()],
            "rms_hop": 512,
        }


register(LibrosaBackend.name, LibrosaBackend)

__all__ = [
    "LibrosaBackend",
    "_bpm_from_beats",
    "_energy_from_rms_dbfs",
    "_estimate_key",
    "estimate_downbeats_from_beats",
]
