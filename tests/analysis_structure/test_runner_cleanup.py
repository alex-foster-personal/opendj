"""Requirements: STRUCT-03.

The All-In-One runner deletes the Demucs stems after every track, and a delete
that fails must fail the track rather than read as ok.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from apps.analysis_structure import allin1_runner


def _fake_allin1(monkeypatch: pytest.MonkeyPatch) -> None:
    def analyze(path, *, demix_dir, spec_dir, **_kw):
        stem_dir = Path(demix_dir) / "htdemucs" / Path(path).stem
        stem_dir.mkdir(parents=True)
        (stem_dir / "drums.wav").write_bytes(b"x")
        Path(spec_dir).mkdir(parents=True)
        return types.SimpleNamespace(segments=[], bpm=120, beats=[], downbeats=[])

    monkeypatch.setitem(sys.modules, "allin1", types.SimpleNamespace(analyze=analyze))
    monkeypatch.setattr(
        allin1_runner, "_envelopes_or_error", lambda _d: {"envelopes": None, "envelopes_error": None}
    )


def test_stems_are_deleted_after_a_track(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_allin1(monkeypatch)
    work = tmp_path / "t0"
    doc = allin1_runner._analyze_one(tmp_path / "song.mp3", "cpu", work)
    assert doc["status"] == "ok"
    # CONTROL for the absence claim: the fake did write a stem under work.
    assert not work.exists()


def test_a_failed_delete_fails_the_track(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_allin1(monkeypatch)
    monkeypatch.setattr(allin1_runner.shutil, "rmtree", lambda *_a, **_k: None)
    work = tmp_path / "t0"
    with pytest.raises(RuntimeError, match="could not delete Demucs byproducts"):
        allin1_runner._analyze_one(tmp_path / "song.mp3", "cpu", work)
    assert (work / "demix" / "htdemucs" / "song" / "drums.wav").is_file()
