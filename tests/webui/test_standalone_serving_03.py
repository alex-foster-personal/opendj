"""STANDALONE-03: empty analysis cells state why they are empty."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.analysis import selection
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.rb_vendor_pkg.track_rows import build_track_rows
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("STANDALONE-03")

STABLE_ID = "b" * 40
MAPPED_SID = "c" * 40
BPM = 121.0
FINGERPRINT = "sha256:" + "cd" * 32


def _beatgrid_record(stable_id: str) -> AnalysisRecord:
    beats = [
        {"t": round(index * 0.5, 5), "n": (index % 4) + 1, "bpm": BPM}
        for index in range(8)
    ]
    payload = {
        "beats": beats,
        "bpm": BPM,
        "bpm_confidence": 0.9,
        "octave_reason": "in_band",
        "first_downbeat_s": 0.0,
        "tempo_changes": [],
        "static_grid_untrusted": False,
    }
    return AnalysisRecord(
        stable_id=stable_id,
        backend="own_beatgrid.backfill",
        backend_version="1.0.0",
        analyzed_at=datetime.now(UTC),
        duration_s=32.0,
        sample_rate=44100,
        bpm=BPM,
        bpm_confidence=0.9,
        key_camelot="",
        key_openkey="",
        key_confidence=0.0,
        energy=0,
        producer="backfill",
        producer_version="1.0.0",
        uses_model=False,
        model_sha256=None,
        decode_fingerprint=FINGERPRINT,
        lanes={"beatgrid": LaneResult(status="ok", payload=payload)},
    )


@pytest.fixture(autouse=True)
def _reset_toggles() -> Iterator[None]:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


def test_empty_bpm_row_carries_status_and_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] no track_fields bpm and no own beatgrid [then] bpm_status+reason, [else stop]."""
    state_path = tmp_path / "state.db"
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        writer.upsert_track(
            stable_id=STABLE_ID,
            stable_id_tier="inferred",
            title="Empty BPM",
            artists=[],
            album=None,
            isrc=None,
            duration_ms=60_000,
            file_path=str(tmp_path / "t.wav"),
        )
    finally:
        writer.close()
        conn.close()
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    track = SqliteBackend(state_path).get_track(STABLE_ID)
    row = build_track_rows([track])[0]
    assert row["bpm"] is None
    assert row["bpm_status"] == "missing"
    assert row["bpm_reason"]


def test_own_record_with_rbx_effective_reports_available_not_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] own ok record exists but rbx effective [then] available-not-selected, [else stop]."""
    state_path = tmp_path / "state.db"
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        writer.upsert_track(
            stable_id=STABLE_ID,
            stable_id_tier="inferred",
            title="Own not selected",
            artists=[],
            album=None,
            isrc=None,
            duration_ms=60_000,
            file_path=str(tmp_path / "t.wav"),
        )
    finally:
        writer.close()
        conn.close()
    upsert_record(_beatgrid_record(STABLE_ID), db_path=state_path)
    selection.set_toggle("beatgrid", "rbx")
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    track = SqliteBackend(state_path).get_track(STABLE_ID)
    row = build_track_rows([track])[0]
    assert row["bpm_status"] == "available-not-selected"
    assert row["bpm_reason"] is not None
    assert "beatgrid" in row["bpm_reason"].lower()
