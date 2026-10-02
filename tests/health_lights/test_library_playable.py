"""The shared playable predicate (HEALTH-01, HEALTH-02).

Regression lines:
  - if an off-machine row is counted as broken then broken
  - if a link this machine recorded as present and then lost is not broken_here then broken
  - if the buckets do not sum to the live row total then broken
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.webui.server import library_playable
from tests.health_lights import fixtures as fx


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    return fx.make_state_db(tmp_path / "data")


def _scan(state_db: Path, mounted: set[str] | None = None) -> library_playable.LibraryPlayability:
    conn = sqlite3.connect(state_db)
    try:
        return library_playable.scan_playability(conn, mounted=mounted or set())
    finally:
        conn.close()


@pytest.mark.requirement("HEALTH-01")
def test_every_row_lands_in_exactly_one_bucket(state_db: Path, tmp_path: Path) -> None:
    """[if] the playable buckets do not sum to the live row total [then] fail, [else stop]."""
    here = fx.audio_file(tmp_path / "music", "here.mp3")
    lost = fx.audio_file(tmp_path / "music", "lost.mp3")
    fx.seed_track(state_db, "present", str(here))
    fx.seed_track(state_db, "lost", str(lost))
    fx.claim_here(state_db, "lost", lost)
    lost.unlink()
    fx.seed_track(state_db, "elsewhere", "/Users/someone-else/Music/elsewhere.mp3")
    fx.claim_on_other_machine(state_db, "elsewhere", "/Users/someone-else/Music/elsewhere.mp3")
    fx.seed_track(state_db, "never-seen", str(tmp_path / "music" / "never-seen.mp3"))
    fx.seed_track(state_db, "drive", "/Volumes/NOT-MOUNTED-HEALTH/drive.mp3")
    fx.seed_track(state_db, "stream", "tidal:12345")
    fx.seed_track(state_db, "pathless", None)

    scan = _scan(state_db)

    assert scan.counts() == {
        "total": 7,
        "present": 1,
        "broken_here": 1,
        "off_machine": 2,
        "awaiting_volume": 1,
        "streaming": 1,
        "pathless": 1,
    }
    assert scan.present == (("present", str(here.resolve())),) or scan.present == (
        ("present", str(here)),
    )
    assert scan.broken_here == ("lost",)


@pytest.mark.requirement("HEALTH-01")
def test_off_machine_rows_are_not_broken(state_db: Path) -> None:
    """A library subset: rows whose audio lives on another machine.

    [if] an off-machine row is counted as broken [then] fail, [else stop].
    """
    for index in range(5):
        path = f"/Users/someone-else/Music/{index}.mp3"
        fx.seed_track(state_db, f"remote-{index}", path)
        fx.claim_on_other_machine(state_db, f"remote-{index}", path)

    scan = _scan(state_db)

    assert scan.broken_here == ()
    assert scan.off_machine == 5


@pytest.mark.requirement("HEALTH-01")
def test_a_genuinely_broken_local_link_stays_broken(state_db: Path, tmp_path: Path) -> None:
    """Overshoot control: 'not broken' must not swallow a real local loss.

    [if] a link recorded present here and then lost is not broken_here [then] fail, [else stop].
    """
    lost = fx.audio_file(tmp_path / "music", "lost.mp3")
    fx.seed_track(state_db, "lost", str(lost))
    fx.claim_here(state_db, "lost", lost)
    assert _scan(state_db).broken_here == ()  # control: resolves while the file exists
    lost.unlink()

    scan = _scan(state_db)

    assert scan.broken_here == ("lost",)
    assert scan.off_machine == 0


@pytest.mark.requirement("HEALTH-01")
def test_soft_deleted_rows_are_in_no_bucket(state_db: Path, tmp_path: Path) -> None:
    """[if] a soft-deleted row lands in any playable bucket [then] fail, [else stop]."""
    here = fx.audio_file(tmp_path / "music", "here.mp3")
    fx.seed_track(state_db, "kept", str(here))
    fx.seed_track(state_db, "deleted", str(here))
    conn = sqlite3.connect(state_db)
    conn.execute("UPDATE tracks SET deleted_at = ? WHERE stable_id = 'deleted'", (fx.STAMP,))
    conn.commit()
    conn.close()

    assert _scan(state_db).counts()["total"] == 1


@pytest.mark.requirement("HEALTH-01")
def test_a_mounted_volume_path_that_is_gone_is_not_awaiting(state_db: Path) -> None:
    """[if] a missing path on a mounted volume counts as awaiting its volume [then] fail, [else stop]."""
    fx.seed_track(state_db, "drive", "/Volumes/MOUNTED-HEALTH/drive.mp3")

    scan = _scan(state_db, mounted={"MOUNTED-HEALTH"})

    assert scan.awaiting_volume == 0
    assert scan.off_machine == 1
