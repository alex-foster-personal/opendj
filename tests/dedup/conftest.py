"""Shared fixtures for the Phase 7 dedup test suite."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from apps.shared import fingerprints as fp_mod

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


class _FakeAcoustid:
    """Deterministic stand-in for pyacoustid.fingerprint_file.

    The fake returns fingerprints whose FIRST 64 hex chars depend only on
    the stem-prefix (``src`` vs ``other``) + duration bucket, so two
    files derived from the same ffmpeg source share those 64 chars. The
    remaining tail hashes the full file bytes -- so cross-bitrate twins
    differ only in the tail, giving compare() a high but <1.0 similarity.
    This matches real chromaprint's cross-bitrate behaviour.
    """

    class NoBackendError(Exception):
        pass

    class FingerprintGenerationError(Exception):
        pass

    def fingerprint_file(self, path: str):  # type: ignore[no-untyped-def]
        data = Path(path).read_bytes()
        try:
            from tinytag import TinyTag

            duration = TinyTag.get(path).duration or 0.0
        except Exception:
            duration = 0.0
        dur_bucket = round(duration)
        stem_key = Path(path).stem.split("-")[0].lower()
        prefix_seed = f"{stem_key}|{dur_bucket}".encode()
        prefix = hashlib.sha256(prefix_seed).hexdigest()[:64]
        tail = hashlib.sha256(data).hexdigest()
        return duration, (prefix + tail).encode("ascii")


@pytest.fixture
def fake_acoustid(monkeypatch):
    fake = _FakeAcoustid()
    monkeypatch.setattr(fp_mod, "_require_acoustid", lambda: fake)
    return fake


@pytest.fixture
def dedup_fixture_root():
    return FIXTURE_ROOT


@pytest.fixture
def tmp_fixture_tree(tmp_path: Path) -> Path:
    """Copy the phase7-dedup fixtures into tmp so tests can mutate mtime."""
    dst = tmp_path / "music"
    dst.mkdir()
    for f in FIXTURE_ROOT.iterdir():
        if f.is_file():
            shutil.copy2(f, dst / f.name)
    return dst
