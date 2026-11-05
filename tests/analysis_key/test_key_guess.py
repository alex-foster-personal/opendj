"""KEY-GUESS-01: a declined key keeps its best guess beside the failed lane.

Regression lines:
  if an ambiguous-margin key record drops its best candidate then broken
  if an ok key record carries a guess then broken
  if load_key_guesses misses a stored guess, or serves one for an ok key, then broken
  if a declined key saved before guesses existed is still "done" for the drain then broken
  if the queue runner skips that record as already current (so the drain and runner disagree) then broken

-Claude
"""
from __future__ import annotations

from pathlib import Path

from apps.analysis.backends import own_key
from apps.analysis.record import validate_record_contract
from apps.analysis.store import open_conn, upsert_record
from apps.analysis_key import canon, flags, key_guess, profiles, segments
from apps.webui.server.ahead_analysis_records import done_ids

FINGERPRINT_HEX = "1f" * 32
A_MINOR = canon.Key(9, True)


def _record(sid: str, *, confidence: float, margin: float):  # type: ignore[no-untyped-def]
    estimate = profiles.KeyEstimate(key=A_MINOR, confidence=confidence, margin=margin)
    return own_key.record_from_estimate(
        stable_id=sid, estimate=estimate, flag=flags.evaluate_tonal_center(estimate),
        duration_s=32.0, sample_rate=44100, decode_fingerprint=FINGERPRINT_HEX,
        segments_block=segments.missing_block(segments.REASON_NO_DOWNBEATS).to_payload(),
    )


def test_an_ambiguous_key_record_keeps_its_best_guess_and_stays_failed() -> None:
    record = _record("amb", confidence=0.9, margin=0.0)
    assert record.lanes["key"].status == "failed", "if a guess is published as a key then broken"
    guess = record.features_blob[key_guess.KEY_GUESS_BLOB]
    assert guess["camelot"] == "8A", "if the best candidate is dropped then broken"
    assert guess["confidence"] == 0.9
    validate_record_contract(record)


def test_an_ok_key_record_carries_no_guess() -> None:
    record = _record("ok", confidence=0.9, margin=0.5)
    assert record.lanes["key"].status == "ok"
    assert key_guess.KEY_GUESS_BLOB not in record.features_blob, "if an ok key carries a guess then broken"


def test_guesses_load_for_declined_rows_and_old_declines_rerun(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    upsert_record(_record("amb", confidence=0.9, margin=0.0), db_path=db)
    upsert_record(_record("ok", confidence=0.9, margin=0.5), db_path=db)
    old = _record("old", confidence=0.9, margin=0.0)
    old.features_blob.clear()  # a declined record saved before KEY-GUESS-01
    upsert_record(old, db_path=db)
    got = key_guess.load_key_guesses(
        db, ["amb", "ok", "old"], backend=key_guess.KEY_BACKEND, version=key_guess.KEY_VERSION
    )
    assert got == {"amb": ("8A", 0.9)}, "if load_key_guesses misses or invents a guess then broken"
    done = done_ids(lambda: open_conn(db), key_guess.KEY_BACKEND)
    assert done == {"amb", "ok"}, "if a pre-guess decline is still done (never re-runs) then broken"


def test_the_runner_does_not_skip_a_pre_guess_decline(tmp_path: Path) -> None:
    from apps.analysis import queue_runner, queue_store

    db = tmp_path / "state.db"
    old = _record("old", confidence=0.9, margin=0.0)
    old.features_blob.clear()
    upsert_record(old, db_path=db)
    upsert_record(_record("amb", confidence=0.9, margin=0.0), db_path=db)
    conn = open_conn(db)
    queue_store.ensure_queue_tables(conn)

    def item(sid: str) -> queue_store.QueueItem:
        return queue_store.QueueItem(
            batch_id="b", stable_id=sid, lane="key", backend=key_guess.KEY_BACKEND, file_path="",
            duration_s=32.0, predicted_peak_mb=None, state="pending", reason=None, attempts=0,
            enqueued_at="2026-11-04T00:00:00Z", started_at=None, finished_at=None, runner_id=None,
        )

    try:
        assert not queue_runner._already_current(conn, item("old"), key_guess.KEY_BACKEND, key_guess.KEY_VERSION), (
            "if the runner skips a pre-guess decline as already current then broken"
        )
        assert queue_runner._already_current(conn, item("amb"), key_guess.KEY_BACKEND, key_guess.KEY_VERSION)
    finally:
        conn.close()
