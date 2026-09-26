"""The NATIVE-10 harness judges by presence, and its real run is one env var away.

- [if] a lane shows no ok measurement [then] the harness fails it, [else stop].

Fast half: ``judge_lane`` against a real sqlite file holding the record shapes
the producers write. Each case is a way a broken install could look fine.

Slow half: the real acceptance (install, deny network, backfill) runs when
``NATIVE10_PAYLOAD``, ``NATIVE10_LIBRARY`` and ``NATIVE10_FFMPEG`` name a built
payload, a small real library and an ffmpeg binary; it skips with that reason
otherwise, because building a 1.4 GB payload is not a unit-test fixture.

Regression lines:
  - if a lane with zero records passes then the harness reads a clean exit as success
  - if a lane where every track is a producer outcome passes then no measurement was ever shown
  - if a reason outside PRODUCER_OUTCOMES passes then an install fault can hide as a judgment
  - if a key without bar-synchronous segments passes then beatgrid can be missing unnoticed
  - if a nonzero drain exit passes then a crashed drain counts as a result
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.native10_offline_acceptance import LaneVerdict, judge_lane

pytestmark = pytest.mark.requirement("NATIVE-10")

REPO_ROOT = Path(__file__).resolve().parents[2]
IDS = ["sid_a", "sid_b"]

BEATGRID_OK = {"status": "ok", "payload": {"beats": [i * 0.5 for i in range(64)], "bpm": 120.0}}
KEY_OK = {"status": "ok", "payload": {"camelot": "4A", "openkey": "9m", "segments": {
    "status": "ok", "segments": [{"start_bar": 0, "end_bar": 16, "key_camelot": "4A"}]}}}
KEY_NO_TONAL = {"status": "failed", "reason": "no_tonal_center: ambiguous_margin", "payload": {}}


def _db(tmp_path: Path, lane: str, blocks: dict[str, dict[str, Any]]) -> Path:
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE analysis (stable_id TEXT, backend TEXT, record_json TEXT)")
        conn.executemany(
            "INSERT INTO analysis VALUES (?, ?, ?)",
            [(sid, f"own_{lane}.backfill", json.dumps({"lanes": {lane: block}}))
             for sid, block in blocks.items()],
        )
    return db


def _judge(tmp_path: Path, lane: str, blocks: dict[str, dict[str, Any]],
           drain_exit: int = 0) -> LaneVerdict:
    verdict = LaneVerdict(lane=lane, tracks_total=len(IDS), drain_exit=drain_exit)
    return judge_lane(_db(tmp_path, lane, blocks), verdict, IDS)


def test_ok_measurement_plus_named_outcome_passes(tmp_path: Path) -> None:
    verdict = _judge(tmp_path, "key", {"sid_a": KEY_OK, "sid_b": KEY_NO_TONAL})
    assert verdict.passed, verdict.failures
    assert (verdict.tracks_ok, verdict.tracks_producer_outcome) == (1, 1)


def test_no_records_fails_even_on_a_clean_drain(tmp_path: Path) -> None:
    verdict = _judge(tmp_path, "waveform", {})
    assert not verdict.passed
    assert len(verdict.failures) == 2


def test_only_producer_outcomes_fails(tmp_path: Path) -> None:
    verdict = _judge(tmp_path, "key", {"sid_a": KEY_NO_TONAL, "sid_b": KEY_NO_TONAL})
    assert not verdict.passed


def test_unlisted_failure_reason_fails(tmp_path: Path) -> None:
    runner_missing = {"status": "failed", "reason": "runner_unavailable", "payload": {}}
    verdict = _judge(tmp_path, "beatgrid", {"sid_a": BEATGRID_OK, "sid_b": runner_missing})
    assert not verdict.passed
    assert "runner_unavailable" in verdict.failures[0]


def test_lane_without_outcomes_admits_none(tmp_path: Path) -> None:
    named_elsewhere = {"status": "failed", "reason": "no_tonal_center: x", "payload": {}}
    ok = {"status": "ok", "payload": {"integrated_lufs": -9.0}}
    verdict = _judge(tmp_path, "loudness", {"sid_a": ok, "sid_b": named_elsewhere})
    assert not verdict.passed


def test_key_without_segments_fails(tmp_path: Path) -> None:
    unsegmented = {"status": "ok", "payload": {"camelot": "4A", "segments": {
        "status": "missing", "reason": "no_own_downbeats", "segments": []}}}
    verdict = _judge(tmp_path, "key", {"sid_a": unsegmented, "sid_b": KEY_NO_TONAL})
    assert not verdict.passed


def test_nonzero_drain_exit_fails(tmp_path: Path) -> None:
    verdict = _judge(tmp_path, "beatgrid", {"sid_a": BEATGRID_OK, "sid_b": BEATGRID_OK},
                     drain_exit=2)
    assert verdict.tracks_ok == 2
    assert not verdict.passed


@pytest.mark.slow
def test_installed_payload_backfills_offline(tmp_path: Path) -> None:
    names = ("NATIVE10_PAYLOAD", "NATIVE10_LIBRARY", "NATIVE10_FFMPEG")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.skip(f"set {missing} to a built payload, a small real library and ffmpeg")
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.native10_offline_acceptance",
         "--payload", os.environ["NATIVE10_PAYLOAD"], "--library", os.environ["NATIVE10_LIBRARY"],
         "--work", str(work), "--ffmpeg", os.environ["NATIVE10_FFMPEG"]],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=7200, check=False,
    )
    assert proc.returncode == 0, f"exit {proc.returncode}\n{proc.stderr[-4000:]}"
