"""Plan uniqueness rules for iCloud path heal."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from apps.reconcile import heal_icloud_paths as heal
from apps.reconcile import locate
from apps.shared import rekordbox_db


def _track(**kwargs: object) -> rekordbox_db.RBTrack:
    defaults = dict(
        id="1",
        title="Hypnosis",
        artist="PALAZZO",
        album="",
        genre="",
        folder_path="/Users/dev/Library/Mobile Documents/com~apple~CloudDocs/x/01 Hypnosis.mp3",
        file_path=None,
        is_streaming=False,
        bpm=None,
        rating=None,
        file_size=5_448_878,
        date_added=None,
        isrc=None,
        duration_s=166.0,
    )
    defaults.update(kwargs)
    return rekordbox_db.RBTrack(**defaults)  # type: ignore[arg-type]


def test_classify_ready_unique_one_basename_plus_size() -> None:
    cand = locate.Candidate(
        path=Path("/Users/dev/Music/Unravelling/01 Hypnosis.mp3"),
        confidence=0.55,
        signals=["basename_exact", "size_match"],
    )
    row = heal._classify(_track(), [cand])
    assert row.status == "ready_unique"
    assert row.candidate_path.endswith("01 Hypnosis.mp3")


def test_classify_needs_confirm_when_two_unique_bar() -> None:
    cands = [
        locate.Candidate(
            path=Path("/Users/dev/Music/a/01 Hypnosis.mp3"),
            confidence=0.55,
            signals=["basename_exact", "size_match"],
        ),
        locate.Candidate(
            path=Path("/Users/dev/Music/b/01 Hypnosis.mp3"),
            confidence=0.55,
            signals=["basename_exact", "duration_match"],
        ),
    ]
    row = heal._classify(_track(), cands)
    assert row.status == "needs_confirm"
    assert "2 unique-bar" in row.rationale


def test_classify_needs_confirm_basename_only() -> None:
    cand = locate.Candidate(
        path=Path("/Users/dev/Music/01 Hypnosis.mp3"),
        confidence=0.35,
        signals=["basename_exact"],
    )
    row = heal._classify(_track(), [cand])
    assert row.status == "needs_confirm"


def test_classify_no_twin() -> None:
    row = heal._classify(_track(), [])
    assert row.status == "no_twin"


@pytest.mark.skipif(sys.platform != "darwin", reason="zone helper Darwin-scoped")
def test_build_plan_finds_music_twin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.shared import audio_files, icloud_zone, paths, platform_paths

    home = tmp_path
    music_root = home / "Music"
    music = music_root / "Unravelling"
    music.mkdir(parents=True)
    twin = music / "01 Hypnosis.mp3"
    twin.write_bytes(b"x" * 4096)

    zone = (
        home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        / "Convert" / "01 Hypnosis.mp3"
    )
    original = str(zone)
    roots = [music_root]

    monkeypatch.setattr(platform_paths, "HOME", home)
    monkeypatch.setattr(platform_paths, "MUSIC_ROOTS", roots)
    monkeypatch.setattr(paths, "MUSIC_ROOTS", roots)
    monkeypatch.setattr(audio_files.paths, "MUSIC_ROOTS", roots)
    monkeypatch.setattr(icloud_zone.platform_paths, "HOME", home)
    monkeypatch.setattr(heal.paths, "MUSIC_ROOTS", roots)

    track = _track(
        folder_path=original,
        file_size=4096,
        duration_s=None,
    )
    rows = heal.build_plan([track])
    assert len(rows) == 1
    assert rows[0].status == "ready_unique"
    assert Path(rows[0].candidate_path) == twin
