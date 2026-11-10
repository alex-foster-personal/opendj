"""Yaml-backed fast lane list for first folder import (issue #11152).

[if] load_config() is read [then] first_import_lanes returns yaml fast lanes, [else stop].
[if] first import enqueues [then] only configured fast lanes get batches, [else stop].
[if] stems or lyrics are requested [then] assert_first_import_lane_allowed refuses, [else stop].
[if] provenance or rip_quality are named [then] they stay off the config list, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.analysis import config as analysis_config
from apps.analysis import queue as queue_api
from apps.analysis import queue_targets
from apps.analysis.queue_user_lanes import FIRST_IMPORT_FORBIDDEN_LANES
from apps.analysis.store import open_conn
from apps.analysis.worker_sizing import HostCapacity

pytestmark = pytest.mark.requirement("ANALYSIS-11968")

AMPLE_HOST = HostCapacity(available_mb=16_000.0, cpu_count=8)


def _seed_track(db: Path, stable_id: str = "import_a") -> None:
    audio = db.parent / f"{stable_id}.wav"
    audio.write_bytes(b"\0")
    conn = open_conn(db)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
        "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, "
        "'2026-11-10T00:00:00Z', '2026-11-10T00:00:00Z')",
        (stable_id, stable_id, 180_000, str(audio)),
    )
    conn.commit()
    conn.close()


def test_first_import_lanes_reads_load_config() -> None:
    """[if] load_config() is read after this change [then] yaml lists fast lanes, [else stop]."""
    analysis_config.load_config.cache_clear()
    calls: list[str] = []
    real = analysis_config.load_config

    def tracked_load(path: str | None = None) -> dict:
        calls.append(path or "default")
        return real(path)

    analysis_config.load_config = tracked_load  # type: ignore[assignment]
    try:
        lanes = analysis_config.first_import_lanes()
    finally:
        analysis_config.load_config = real  # type: ignore[assignment]
        real.cache_clear()
    assert lanes == ("loudness", "beatgrid", "key", "waveform")
    assert calls == ["default"]


def test_first_import_enqueues_analysis_for_a_new_track(tmp_path: Path) -> None:
    """[if] first import enqueues analysis for a new track [then] only yaml lanes run, [else stop]."""
    db = tmp_path / "state.db"
    _seed_track(db)
    conn = open_conn(db)
    plan = queue_api.enqueue_first_import(
        conn,
        ["import_a"],
        queue_targets.candidates_from_state,
        host=AMPLE_HOST,
    )
    assert plan.lanes == analysis_config.first_import_lanes()
    assert len(plan.results) == len(plan.lanes)
    for enq in plan.results:
        assert enq.admitted == 1
        assert enq.refused == 0
    conn.close()


@pytest.mark.parametrize("lane", ["stems", "lyrics"])
def test_first_import_rejects_user_job_lane_requests(lane: str) -> None:
    """[if] a caller asks the first-import list for stems or lyrics [then] it refuses, [else stop]."""
    with pytest.raises(analysis_config.FirstImportLaneError, match=lane):
        analysis_config.assert_first_import_lane_allowed(lane)


def test_first_import_list_excludes_provenance_and_rip_quality() -> None:
    """[if] provenance/rip-quality is absent from the first-import list [then] stay forbidden, [else stop]."""
    lanes = analysis_config.first_import_lanes()
    for deferred in ("provenance", "rip_quality"):
        assert deferred in FIRST_IMPORT_FORBIDDEN_LANES
        assert deferred not in lanes
        with pytest.raises(analysis_config.FirstImportLaneError, match=deferred):
            analysis_config.assert_first_import_lane_allowed(deferred)
