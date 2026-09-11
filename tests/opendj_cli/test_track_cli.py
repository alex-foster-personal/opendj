"""``opendj track key-segments <stable_id>``: the CLI parity surface for
nav1-key-record's `key_segments` block.

specs/native-analysis-v1-lanes/nav1-key-record.md item 3: "prints the same
`key_segments` block (status, reason, segments) the `/anlz` payload carries,
with an end-to-end test that writes a two-segment record, runs the command,
and checks the printed block against the wire payload."

Never touches the AGENT-03 engine bus: no lock file, no running deck engine.

  [if] the CLI prints a different block than the wire overlay [then] broken,
  [else stop]

-Claude
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.analysis import selection
from apps.analysis.backends.own_key import record_from_estimate
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import open_conn, upsert_record
from apps.analysis_key import canon, flags, profiles, segments
from apps.analysis_key.lane_payload import depends_on_identity
from apps.webui.server.rb_vendor_pkg.own_key_overlay import apply_own_key_segments

REPO_ROOT = Path(__file__).resolve().parents[2]
C_MAJOR = canon.Key(0, False)
A_MINOR = canon.Key(9, True)
FINGERPRINT_HEX = "3f" * 32
FINGERPRINT = "sha256:" + FINGERPRINT_HEX


@pytest.fixture(autouse=True)
def _reset_toggles() -> Any:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    open_conn(db_path).close()
    return db_path


def _run_track_cli(argv: list[str], *, data_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MDT_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "apps.opendj_cli", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )


def _bar_chroma(keys: list[canon.Key]) -> Any:
    import numpy as np

    columns = []
    for key in keys:
        third = 3 if key.is_minor else 4
        vector = np.zeros(12)
        for interval in (0, third, 7):
            vector[(key.pitch_class + interval) % 12] = 1.0
        columns.append(vector / np.linalg.norm(vector))
    return np.stack(columns, axis=1)


def _two_segment_block() -> dict[str, Any]:
    grid = segments.BarGrid(
        starts=tuple(float(bar) * 2.0 for bar in range(32)),
        ends=tuple(float(bar) * 2.0 for bar in range(1, 33)),
    )
    block = segments.segment_bars(
        _bar_chroma([C_MAJOR] * 16 + [A_MINOR] * 16), grid, duration_s=64.0
    )
    assert block.status == "ok" and len(block.segments) == 2, block
    return block.to_payload()


def _matching_beatgrid_record(stable_id: str) -> AnalysisRecord:
    from datetime import UTC, datetime

    beats = [
        {
            "t": round(index * 0.5, 5),
            "n": (index % 4) + 1,
            "bpm": 120.0,
        }
        for index in range(32)
    ]
    payload = {
        "beats": beats,
        "bpm": 120.0,
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
        duration_s=64.0,
        sample_rate=44100,
        bpm=120.0,
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


def _seed_two_segment_record(state_db: Path, stable_id: str) -> None:
    grid = _matching_beatgrid_record(stable_id)
    upsert_record(grid, db_path=state_db)
    depends_on = depends_on_identity(
        backend=grid.backend,
        producer_version=grid.producer_version,
        model_sha256=grid.model_sha256,
        decode_fingerprint=grid.decode_fingerprint,
        beatgrid_payload=grid.lanes["beatgrid"].payload,
    )
    estimate = profiles.KeyEstimate(key=C_MAJOR, confidence=0.9, margin=0.4)
    record = record_from_estimate(
        stable_id=stable_id,
        estimate=estimate,
        flag=flags.evaluate_tonal_center(estimate),
        duration_s=64.0,
        sample_rate=44100,
        decode_fingerprint=FINGERPRINT_HEX,
        segments_block=_two_segment_block(),
        depends_on_beatgrid=depends_on,
    )
    upsert_record(record, db_path=state_db)
    conn = open_conn(state_db)
    try:
        selection.set_default(conn, "key", "own")
    finally:
        conn.close()


def test_key_segments_prints_the_same_block_the_wire_payload_carries(
    state_db: Path,
) -> None:
    stable_id = "sid-cli-key-segments"
    _seed_two_segment_record(state_db, stable_id)
    data_dir = state_db.parent.parent

    done = _run_track_cli(
        ["track", "key-segments", stable_id, "--json"], data_dir=data_dir
    )
    printed = json.loads(done.stdout)

    assert done.returncode == 0
    wire_block = apply_own_key_segments(
        {}, stable_id, state_db_path=state_db
    )["key_segments"]
    assert printed == wire_block
    assert printed["status"] == "ok"
    assert len(printed["segments"]) == 2


def test_key_segments_with_no_own_record_reports_missing(state_db: Path) -> None:
    conn = open_conn(state_db)
    try:
        selection.set_default(conn, "key", "own")
    finally:
        conn.close()

    done = _run_track_cli(
        ["track", "key-segments", "sid-no-record", "--json"],
        data_dir=state_db.parent.parent,
    )
    printed = json.loads(done.stdout)

    assert done.returncode == 0
    assert printed["status"] == "missing"
    assert printed["segments"] == []


def test_track_rejects_an_unknown_verb() -> None:
    done = _run_track_cli(["track"], data_dir=Path("/tmp"))
    assert done.returncode != 0
