"""Shared fixtures for the Phase 7 dedup test suite."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apps.shared import fingerprints as fp_mod
from tests.fingerprint_fakes import fake_fingerprint

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


class _FakeAcoustid:
    """Deterministic stand-in for pyacoustid.fingerprint_file.

    The fake returns real-format fingerprints whose leading sub-fingerprints
    depend only on the stem-prefix (``src`` vs ``other``) + duration bucket,
    so two files derived from the same ffmpeg source share them. The tail
    hashes the full file bytes -- so cross-bitrate twins differ only in the
    tail, giving compare() a high but <1.0 similarity, as real chromaprint
    does across bitrates.
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
        return duration, fake_fingerprint(prefix_seed, data).encode("ascii")


@pytest.fixture
def fake_acoustid(monkeypatch):
    fake = _FakeAcoustid()
    # The fake stands in for the whole backend: a local engine build must
    # not answer instead of it.
    monkeypatch.setattr(fp_mod, "_engine_binary", lambda: None)
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
