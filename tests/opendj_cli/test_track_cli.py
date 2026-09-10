"""``opendj track key-segments <stable_id>``: the CLI parity surface for
nav1-key-record's `key_segments` block.

specs/native-analysis-v1-lanes/nav1-key-record.md item 3: "prints the same
`key_segments` block (status, reason, segments) the `/anlz` payload carries,
with an end-to-end test that writes a two-segment record, runs the command,
and checks the printed block against the wire payload."

Never touches the AGENT-03 engine bus: no lock file, no running deck engine.

-Claude
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.analysis import selection
from apps.analysis.backends.own_key import record_from_estimate
from apps.analysis.store import open_conn, upsert_record
from apps.analysis_key import canon, flags, profiles, segments
from apps.opendj_cli.__main__ import main
from apps.webui.server.rb_vendor_pkg.own_key_overlay import apply_own_key_segments

C_MAJOR = canon.Key(0, False)
A_MINOR = canon.Key(9, True)
FINGERPRINT_HEX = "3f" * 32
DEPENDS_ON_BEATGRID = {
    "backend": "own_beatgrid.backfill",
    "producer_version": "1.0.0",
    "model_sha256": None,
    "decode_fingerprint": "sha256:" + FINGERPRINT_HEX,
    "record_digest": "sha256:" + "cc" * 32,
}


@pytest.fixture(autouse=True)
def _reset_toggles() -> Any:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    open_conn(db_path).close()
    monkeypatch.setattr(rb_config, "STATE_DB", db_path)
    return db_path


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


def test_key_segments_prints_the_same_block_the_wire_payload_carries(
    state_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stable_id = "sid-cli-key-segments"
    estimate = profiles.KeyEstimate(key=C_MAJOR, confidence=0.9, margin=0.4)
    record = record_from_estimate(
        stable_id=stable_id,
        estimate=estimate,
        flag=flags.evaluate_tonal_center(estimate),
        duration_s=64.0,
        sample_rate=44100,
        decode_fingerprint=FINGERPRINT_HEX,
        segments_block=_two_segment_block(),
        depends_on_beatgrid=DEPENDS_ON_BEATGRID,
    )
    upsert_record(record, db_path=state_db)
    conn = open_conn(state_db)
    try:
        selection.set_default(conn, "key", "own")
    finally:
        conn.close()

    exit_code = main(["track", "key-segments", stable_id, "--json"])
    printed = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    wire_block = apply_own_key_segments({}, stable_id, state_db_path=state_db)["key_segments"]
    assert printed == wire_block
    assert printed["status"] == "ok"
    assert len(printed["segments"]) == 2


def test_key_segments_with_no_own_record_reports_missing(
    state_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = open_conn(state_db)
    try:
        selection.set_default(conn, "key", "own")
    finally:
        conn.close()

    exit_code = main(["track", "key-segments", "sid-no-record", "--json"])
    printed = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert printed["status"] == "missing"
    assert printed["segments"] == []


def test_track_with_no_verb_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["track"])
    assert exit_code == 1
    assert "usage: opendj track key-segments" in capsys.readouterr().err
