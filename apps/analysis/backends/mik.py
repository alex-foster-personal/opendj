"""Optional Mixed In Key CLI backend.

If ``mixed-in-key-cli`` is not on PATH the backend still registers but
``analyze()`` raises :class:`BackendNotAvailable`.  Callers should treat
that as "skip this backend".
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..record import AnalysisRecord
from . import register
from .base import BackendNotAvailable, TrackUnreadable

_BIN = "mixed-in-key-cli"
_TIMEOUT_S = 120


class MikBackend:
    name: str = "mik"
    version: str = "mik-cli-unknown"

    @classmethod
    def _binary_path(cls) -> str | None:
        return shutil.which(_BIN)

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        """No in-process JIT, so no cache to fingerprint and none to protect."""
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        """Nothing to warm: every analysis is a subprocess of an external CLI.

        Stated rather than silently inherited, so an empty warm-up here is a
        recorded fact about this backend and not an oversight. If this backend
        ever grows an in-process numba path, this is the method that must
        stop being a no-op.
        """
        return "no in-process JIT; mixed-in-key-cli runs out of process"

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        binary = cls._binary_path()
        if not binary:
            raise BackendNotAvailable(
                f"{_BIN!r} not found on PATH; install Mixed In Key or use "
                "--backend librosa."
            )
        result = subprocess.run(
            [binary, "--json", str(path)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
        if result.returncode != 0:
            # The tool ran; it rejected this file. That is per-file, unlike a
            # tool that is missing (BackendNotAvailable, above).
            raise TrackUnreadable(
                f"mixed-in-key-cli failed rc={result.returncode}: "
                f"{result.stderr.strip()[:200]}"
            )
        data: dict[str, Any] = json.loads(result.stdout)

        return AnalysisRecord(
            stable_id=stable_id,
            backend=cls.name,
            backend_version=data.get("version", cls.version),
            analyzed_at=datetime.now(UTC),
            duration_s=float(data.get("duration_s", 0.0)),
            sample_rate=int(data.get("sample_rate", 44100)),
            bpm=float(data.get("bpm", 0.0)),
            bpm_confidence=float(data.get("bpm_confidence", 1.0)),
            key_camelot=str(data.get("key_camelot", "1A")),
            key_openkey=str(data.get("key_openkey", "1m")),
            key_confidence=float(data.get("key_confidence", 1.0)),
            energy=int(data.get("energy", 5)),
            energy_source="mik",
            onsets_s=list(data.get("onsets_s", [])),
            downbeats_s=list(data.get("downbeats_s", [])),
            rms_peaks_s=list(data.get("rms_peaks_s", [])),
            features_blob=dict(data.get("features_blob", {})),
        )


register(MikBackend.name, MikBackend)
