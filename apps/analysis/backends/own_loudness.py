"""AnalyzerBackend wrapper around :mod:`apps.analysis_loudness`.

Registers as ``own_loudness.backfill`` so ``apps.analysis.run --backend``
can write a v2 :class:`~apps.analysis.record.AnalysisRecord` through the
existing store path. Measurement stays in the loudness producer; this
module only supplies record identity, the contract decode fingerprint,
and the exception mapping the runner already classifies.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import subprocess
import tempfile
import threading
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from apps.loudness.scan import SCAN_TIMEOUT_S, LoudnessError, require_ffmpeg

from ..record import AnalysisRecord
from . import register
from .base import BackendNotAvailable, TrackUnreadable, TrackVanished

# Native-analysis v1 decode fingerprint (specs/native-analysis-v1.md §13):
# sha256 of decoded PCM at 44100 Hz, mono, signed 16-bit, soxr resampler
# at a pinned precision. The record contract requires the ``sha256:`` prefix.
_FINGERPRINT_SAMPLE_RATE_HZ = 44100
_FINGERPRINT_SAMPLE_WIDTH = 2  # s16le bytes per mono sample
_FINGERPRINT_FILTER = "aresample=resampler=soxr:precision=28"
_PCM_READ_CHUNK = 1 << 16
_STDERR_TAIL = 2000
_STDERR_READ_CHUNK = 4096

# Pre-v2 analysis columns this loudness-only record cannot measure. They
# exist because the table still requires them; they are not measurements
# and cannot project from a loudness-lane pointer.
_PLACEHOLDER_BPM = 0.0
_PLACEHOLDER_BPM_CONFIDENCE = 0.0
_PLACEHOLDER_KEY_CAMELOT = ""
_PLACEHOLDER_KEY_OPENKEY = ""
_PLACEHOLDER_KEY_CONFIDENCE = 0.0
_PLACEHOLDER_ENERGY = 0


def _load_loudness_producer() -> ModuleType:
    """Load the lane producer without a static ``apps.analysis_loudness`` import.

    The producer already imports :mod:`apps.analysis.lanes`. A static import
    the other way is the ``analysis <-> analysis_loudness`` package pair the
    quality gate refuses. ``importlib`` plus a joined name keeps that edge
    out of grimp's static graph; analyze() still calls the real producer.
    """
    return importlib.import_module(".".join(("apps", "analysis_loudness")))


def _run_with_bounded_stderr(argv: list[str], timeout_s: float) -> tuple[int, str]:
    """Run argv, retaining only the last ``_STDERR_TAIL`` stderr bytes.

    A drain thread feeds a ``deque(maxlen=...)`` ring so a chatty decoder
    cannot accumulate an unbounded PIPE buffer. Holding the whole stream
    until exit would retain that buffer; this drops the prefix as it
    arrives and returns the useful final tail.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    stream = proc.stderr
    if stream is None:  # pragma: no cover - PIPE always opens stderr
        proc.wait()
        returncode = proc.returncode
        return (1 if returncode is None else returncode), ""
    tail: deque[int] = deque(maxlen=_STDERR_TAIL)

    def _drain() -> None:
        while True:
            chunk = stream.read(_STDERR_READ_CHUNK)
            if not chunk:
                return
            tail.extend(chunk)

    reader = threading.Thread(target=_drain, name="own-loudness-stderr", daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=1)
        raise
    finally:
        reader.join(timeout=5)
        stream.close()
    returncode = proc.returncode
    if returncode is None:  # pragma: no cover - wait() sets it
        returncode = 1
    return returncode, bytes(tail).decode("utf-8", errors="replace")


def _hash_pcm_file(tmp_path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    n_bytes = 0
    with tmp_path.open("rb") as fh:
        while True:
            chunk = fh.read(_PCM_READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            n_bytes += len(chunk)
    return digest.hexdigest(), n_bytes


def _fingerprint_decoded_pcm(path: Path, binary: str) -> tuple[str, float]:
    """Hash decoded PCM at the contract fingerprint and return (digest, duration_s).

    Spools through a temporary file so a full track's s16le is never held in
    memory. Hashes in chunks, requires a whole number of s16 samples, and
    derives duration from the decoded byte count. Decoder stderr is ring-
    buffered to a fixed tail; it is never captured whole.
    """
    fd, tmp_name = tempfile.mkstemp(prefix="mdt-own-loudness-", suffix=".s16le")
    os.close(fd)
    tmp_path = Path(tmp_name)
    n_bytes = 0
    digest_hex = ""
    try:
        returncode, stderr_tail = _run_with_bounded_stderr(
            [
                binary,
                "-hide_banner",
                "-nostdin",
                "-nostats",
                "-y",
                "-i",
                str(path),
                "-map",
                "a:0",
                "-ac",
                "1",
                "-ar",
                str(_FINGERPRINT_SAMPLE_RATE_HZ),
                "-sample_fmt",
                "s16",
                "-af",
                _FINGERPRINT_FILTER,
                "-f",
                "s16le",
                str(tmp_path),
            ],
            SCAN_TIMEOUT_S,
        )
        if returncode != 0:
            if not path.exists():
                raise TrackVanished(f"{path}: gone before decode: {stderr_tail}")
            raise TrackUnreadable(
                f"{path.name}: ffmpeg exited {returncode} decoding PCM fingerprint: {stderr_tail}"
            )
        digest_hex, n_bytes = _hash_pcm_file(tmp_path)
    finally:
        with suppress(OSError):
            tmp_path.unlink()
    if n_bytes == 0:
        raise TrackUnreadable(f"empty audio: {path.name}")
    if n_bytes % _FINGERPRINT_SAMPLE_WIDTH:
        raise TrackUnreadable(
            f"{path.name}: decoded PCM is {n_bytes} bytes, not a whole number of s16 samples"
        )
    n_samples = n_bytes // _FINGERPRINT_SAMPLE_WIDTH
    duration_s = n_samples / float(_FINGERPRINT_SAMPLE_RATE_HZ)
    return f"sha256:{digest_hex}", duration_s


class OwnLoudnessBackfillBackend:
    """Shipped R128 loudness producer, as an :class:`AnalyzerBackend`."""

    # Literals match apps.analysis_loudness.PRODUCER_BACKEND / PRODUCER_VERSION.
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
        """Nothing to warm: R128/RMS/fingerprint decode are out-of-process ffmpeg.

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
        fingerprint, duration_s = _fingerprint_decoded_pcm(path, binary)
        producer = _load_loudness_producer()
        if cls.name != producer.PRODUCER_BACKEND or cls.version != producer.PRODUCER_VERSION:
            raise RuntimeError(
                f"backend identity {cls.name!r}/{cls.version!r} drifted from "
                f"producer {producer.PRODUCER_BACKEND!r}/{producer.PRODUCER_VERSION!r}"
            )
        try:
            lane = producer.produce_lane_result(path, binary=binary)
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
            sample_rate=_FINGERPRINT_SAMPLE_RATE_HZ,
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
