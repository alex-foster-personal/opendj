"""LIBM-128: the real engine lifespan, real folder, real state.db.

One end-to-end test proving the seam ``apps/engine_core/app.py`` wires
together for real: a booted app with a folder import already on record
starts ``FolderRescanScheduler`` (``apps/engine_core/setup/folder_rescan_scheduler.py``),
which finds a new file with no wizard re-run, writes it, and surfaces the
cycle on ``GET /api/v1/setup/status``. No mocks, no injected fast config --
this uses the SAME ``FolderRescanCfg.INITIAL_DELAY_S`` (20s) that ships to
users, so the real bounded interval this PR documents is what the test
waits on.

  - [if] a folder lands after boot [then] it appears live with no restart, [else stop].
"""
from __future__ import annotations

import struct
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.engine_core.app import create_app
from apps.engine_core.config import EngineConfig
from apps.engine_core.setup import importer as setup_importer
from apps.shared.state import db as state_db
from tests.waits import THREAD_HANG_GUARD_S

pytestmark = pytest.mark.requirement("LIBM-128")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    (target / "state").mkdir(parents=True)
    return target


@pytest.fixture
def engine_env(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MDT_LIBRARY_MODE", "local")


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<h", 0))


def _live_track_paths(state_db_path: Path) -> list[str]:
    conn = state_db.open_rw(state_db_path)
    try:
        rows = conn.execute(
            "SELECT file_path FROM tracks WHERE deleted_at IS NULL"
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def test_a_new_file_appears_via_the_real_booted_scheduler_with_no_restart(
    data_dir: Path, tmp_path: Path, engine_env: None
) -> None:
    music_root = tmp_path / "music"
    music_root.mkdir()
    # A real completed import (0 files, an empty folder) establishes the
    # baseline the same way the wizard would: LIBM-41 exempts a 0-prior root
    # from the mass-missing guard, so this is what a genuine first run looks
    # like, not a shortcut around it.
    setup_importer.run_folder_import(data_dir, emit=lambda *_: None, roots=[music_root])
    state_db_path = data_dir / "state" / "state.db"

    app = create_app(EngineConfig(data_dir=data_dir))
    with TestClient(app) as client:
        # Dropped in AFTER boot: the acceptance criterion is "no manual
        # re-run of setup or app restart required".
        _write_wav(music_root / "new-track.wav")

        deadline = time.monotonic() + THREAD_HANG_GUARD_S
        status = {}
        while time.monotonic() < deadline:
            status = client.get("/api/v1/setup/status").json()
            watch = status.get("folder_watch")
            if watch and watch.get("last_cycle_at") is not None:
                break
            time.sleep(0.5)
        else:
            raise AssertionError(
                f"folder-rescan scheduler never completed a cycle within "
                f"{THREAD_HANG_GUARD_S}s; last status: {status}"
            )

        deadline = time.monotonic() + THREAD_HANG_GUARD_S
        while time.monotonic() < deadline:
            if _live_track_paths(state_db_path):
                break
            time.sleep(0.5)

    live_paths = _live_track_paths(state_db_path)
    assert live_paths == [str(music_root / "new-track.wav")]
    assert status["folder_watch"]["warning"] is None
