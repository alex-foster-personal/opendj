"""--seed-autoplay-hunt builds a 6-track library without leaking into FIXTURE_TRACKS.

Issue #1853: the AutoPlay/mixing error hunt needs >=6 real ingested tracks and
two playlists, as a SEPARATE opt-in from --seed-autoplay-chain so the five
other e2e configs sharing this builder keep their 2-track shape.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.webui.frontend.tests.e2e.support.deckload_fixture import (
    FIXTURE_TRACKS,
    HUNT_CAMELOT_KEY,
    HUNT_PLAYLIST_A_ID,
    HUNT_PLAYLIST_B_ID,
    HUNT_TRACKS,
    build,
    build_autoplay_hunt,
    main,
)


def _fields(state_db: Path, field_name: str) -> dict[str, object]:
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        return {
            str(stable_id): json.loads(value_json)
            for stable_id, value_json in conn.execute(
                "SELECT stable_id, value_json FROM track_fields WHERE field_name = ?",
                (field_name,),
            )
        }
    finally:
        conn.close()


def _membership(state_db: Path, playlist_id: str) -> list[str]:
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        return [
            str(stable_id)
            for (stable_id,) in conn.execute(
                "SELECT stable_id FROM playlist_memberships "
                "WHERE playlist_id = ? ORDER BY position",
                (playlist_id,),
            )
        ]
    finally:
        conn.close()


def _playlist_names(state_db: Path) -> dict[str, str]:
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        return {
            str(playlist_id): str(name)
            for playlist_id, name in conn.execute(
                "SELECT playlist_id, name FROM playlists"
            )
        }
    finally:
        conn.close()


def test_seed_autoplay_hunt_produces_six_tagged_tracks_and_two_playlists(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "hunt"
    rows = build_autoplay_hunt(data_dir)
    assert len(HUNT_TRACKS) >= 6
    assert len(rows) >= 6
    state_db = data_dir / "state" / "state.db"
    keys = _fields(state_db, "key")
    bpms = _fields(state_db, "bpm")
    assert set(keys.values()) == {HUNT_CAMELOT_KEY}
    assert set(bpms.values()) == {124.0, 128.0}
    names = _playlist_names(state_db)
    assert names[HUNT_PLAYLIST_A_ID] == "E2E AutoPlay Hunt A"
    assert names[HUNT_PLAYLIST_B_ID] == "E2E AutoPlay Hunt B"
    membership_a = _membership(state_db, HUNT_PLAYLIST_A_ID)
    membership_b = _membership(state_db, HUNT_PLAYLIST_B_ID)
    assert len(membership_a) == 6
    assert membership_b == list(reversed(membership_a))


def test_seed_autoplay_hunt_is_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / "hunt"
    first = build_autoplay_hunt(data_dir)
    second = build_autoplay_hunt(data_dir)
    assert [row[0] for row in first] == [row[0] for row in second]
    assert len(second) >= 6


def test_relative_data_dir_still_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--data-dir", "relative/hunt", "--seed-autoplay-hunt"])
    assert "must be absolute" in str(caught.value)


def test_plain_build_still_yields_two_tracks(tmp_path: Path) -> None:
    assert len(FIXTURE_TRACKS) == 2
    rows = build(tmp_path / "plain")
    assert len(rows) == 2
