"""Shared phase7 dedup test helpers (not conftest -- CI fast-tier treats conftest edits as global)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.dedup import scan as scan_mod
from apps.shared import fingerprints as fp_mod

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"
_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac"}
PHASE7_DEDUP_AUDIO_COUNT = sum(
    1 for path in FIXTURE_ROOT.iterdir() if path.is_file() and path.suffix.lower() in _AUDIO_SUFFIXES
)


class _MissingChromaprintBackend:
    """Stand-in acoustid backend that always reports fpcalc missing."""

    class NoBackendError(Exception):
        pass

    def fingerprint_file(self, _path: str) -> None:
        raise self.NoBackendError("fpcalc missing")


def patch_chromaprint_backend_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``apps.shared.fingerprints`` down the missing-chromaprint path."""
    monkeypatch.setattr(fp_mod, "_engine_binary", lambda: None)
    monkeypatch.setattr(fp_mod, "_require_acoustid", lambda: _MissingChromaprintBackend())


def seed_phase7_scan_db(tmp_path: Path, fixture_tree: Path) -> Path:
    """Populate a dedup sqlite DB by scanning the phase7 fixture tree."""
    db = tmp_path / "phase7.sqlite"
    scan_mod.run_scan(roots=[fixture_tree], db_path=db)
    return db
