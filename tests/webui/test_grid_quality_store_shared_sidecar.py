"""The grid-quality sidecar is shared by two stores; a read must survive the other's table alone.

`grid_quality_store` and `grid_audio_fit_store` both live in `<state dir>/grid-quality.db`.
On a fresh data dir whichever scan runs first creates the file with only its own table, and
the library listing reads grid verdicts for every row on a read-only connection. Found on
every fresh CI runner, Fri 30 Oct 2026 (PR #7948): the AutoPlay gate's `/api/v1/tracks`
failed 500 with `no such table: grid_quality` and no track row ever rendered.

[if] the sidecar holds only the other store's table [then] a read returns no verdicts, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.webui.server import grid_audio_fit_store, grid_quality_store

pytestmark = pytest.mark.requirement("GRIDFIT-02")


def _sidecar_with_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str) -> Path:
    """The shared file as the OTHER store leaves it on a fresh data dir: one table, not ours."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(rb_config, "STATE_DB", state_dir / "state.db")
    sidecar = state_dir / grid_quality_store.STORE_FILENAME
    conn = sqlite3.connect(sidecar)
    with conn:
        conn.execute(schema)
    conn.close()
    return sidecar


def test_a_sidecar_holding_only_the_audio_fit_table_reads_as_no_grid_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the audio-fit scan created the sidecar first [then] grid verdicts read as absent, [else stop]."""
    sidecar = _sidecar_with_only(tmp_path, monkeypatch, grid_audio_fit_store._SCHEMA)
    assert sidecar.is_file() and grid_quality_store.store_path() == sidecar, "control: the stores share the file"
    assert grid_quality_store.read_many(["t1", "t2"]) == {}


def test_a_sidecar_holding_only_the_grid_quality_table_reads_as_no_audio_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the grid scan created the sidecar first [then] audio-fit verdicts read as absent, [else stop]."""
    sidecar = _sidecar_with_only(tmp_path, monkeypatch, grid_quality_store._SCHEMA)
    assert grid_audio_fit_store.store_path() == sidecar
    assert grid_audio_fit_store.read_many(["t1"]) == {}
    assert grid_quality_store.read_many(["t1"]) == {}, "control: an empty own table reads as absent too"
