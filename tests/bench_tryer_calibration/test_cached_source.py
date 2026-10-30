"""Cached profile snapshots (D10): calibration builds read saved engine documents, never a running app."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.autodj.profile import profile_from_payloads
from apps.transition_score.tryer import PairNotTryable
from scripts.bench.tryer_calibration.cached_source import (
    SNAPSHOT_SCHEMA,
    CachedProfileMissing,
    CachedProfiles,
    snapshot,
)
from tests.autodj.fixtures import anlz_payload, track_row


def _save(directory: Path, stable_id: str, anlz: dict, track: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    document = {"schema": SNAPSHOT_SCHEMA, "stable_id": stable_id, "track": track, "anlz": anlz}
    (directory / f"{stable_id}.json").write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.requirement("TRYER-22")
def test_a_snapshot_builds_exactly_the_engine_profile(tmp_path: Path) -> None:
    """[if] a snapshot holds a track's two engine documents [then] its profile equals the engine-built one, [else stop]."""
    anlz, track = anlz_payload("trk-a", bpm=124.0), track_row("trk-a", bpm=124.0, key="9A")
    _save(tmp_path, "trk-a", anlz, track)
    assert CachedProfiles(tmp_path).profile("trk-a") == profile_from_payloads(anlz, track), (
        "if the cached profile differs from the engine-built one then calibration scores a different track"
    )


@pytest.mark.requirement("TRYER-22")
def test_a_missing_snapshot_is_refused_naming_the_command(tmp_path: Path) -> None:
    """[if] no snapshot exists for a track [then] CachedProfileMissing names it and the snapshot command, [else stop].

    Never a default profile or a silent skip."""
    _save(tmp_path, "trk-a", anlz_payload("trk-a"), track_row("trk-a"))
    with pytest.raises(CachedProfileMissing) as missing:
        CachedProfiles(tmp_path).profile("trk-b")
    assert "trk-b" in str(missing.value) and "cached_source snapshot" in str(missing.value)


@pytest.mark.requirement("TRYER-22")
def test_a_snapshot_with_no_usable_grid_skips_the_pair_like_the_engine(tmp_path: Path) -> None:
    """[if] the saved analysis has no beat grid [then] PairNotTryable, as EngineProfiles raises, [else stop]."""
    anlz = anlz_payload("trk-a")
    anlz["beatgrid"] = {"source": "rekordbox", "beat_count": 0, "beats": []}
    _save(tmp_path, "trk-a", anlz, track_row("trk-a"))
    with pytest.raises(PairNotTryable):
        CachedProfiles(tmp_path).profile("trk-a")


def test_snapshot_refuses_before_any_request_when_one_exists(tmp_path: Path) -> None:
    """[if] any target snapshot exists [then] FileExistsError before the engine lock is even read, [else stop]."""
    _save(tmp_path, "trk-a", anlz_payload("trk-a"), track_row("trk-a"))
    with pytest.raises(FileExistsError):
        snapshot(["trk-b", "trk-a"], tmp_path / "no-such.lock", tmp_path)
    assert not (tmp_path / "trk-b.json").exists(), "if the refusal wrote the first track then it was not up front"
