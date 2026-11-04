"""A key computed on a beatgrid that was re-analyzed is re-run, not skipped (NATIVE-36).

Production order is write-then-cascade: the queue runner upserts the new
beatgrid, whose canonical refresh already drops the stale key from the key
pointer, and only then runs the cascade. The cascade used to read that empty
pointer as "no key record", re-queue without marking the old row stale, and
the runner then settled the re-queue as ``skipped: already_current_version``.
Found live Tue 3 Nov 2026: 20 of 31 tracks of one playlist had a blank key.

  - if the cascade runs after the grid write [then] the old key row is marked
    stale, so the re-queue runs - else broken
  - if a dependency-stale key row carries no stale marker [then] the runner
    does not treat it as current - else broken
  - if a key row matches the canonical grid [then] the runner still skips it - else broken
  - if a key is dependency-stale [then] the ahead drain's done set omits it - else broken
  - if a key is current [then] the ahead drain's done set keeps it - else broken

[if] a beatgrid is re-analyzed after its key was written [then] the key is re-run, [else stop].

-Claude
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.analysis import queue_store
from apps.analysis.backends.own_key import BACKEND_NAME, PRODUCER_VERSION
from apps.analysis.canonical import canonical_pointer
from apps.analysis.queue import cascade_dependents
from apps.analysis.queue_runner import _already_current
from apps.analysis.store import open_conn, upsert_record
from apps.webui.server.ahead_analysis_records import done_ids

from .test_key_depends_on_staleness import (
    FINGERPRINT_V1,
    _beatgrid_record,
    _depends_on_for,
    _key_record,
    state_db,  # noqa: F401  (fixture, referenced by name below)
)

pytestmark = pytest.mark.requirement("NATIVE-36")


def _key_item(stable_id: str) -> queue_store.QueueItem:
    return queue_store.QueueItem(
        batch_id="qb_test", stable_id=stable_id, lane="key", backend=BACKEND_NAME,
        file_path="/x.mp3", duration_s=None, predicted_peak_mb=None, state="running",
        reason=None, attempts=1, enqueued_at="", started_at=None, finished_at=None,
        runner_id=None,
    )


def _key_on_regridded_track(db: Path, stable_id: str, *, cascade: bool) -> None:
    """Key written on grid A, then grid B written FIRST and the cascade after."""
    grid_a = _beatgrid_record(stable_id, backend_version="1.6.0", decode_fingerprint=FINGERPRINT_V1)
    upsert_record(grid_a, db_path=db)
    upsert_record(_key_record(stable_id, depends_on_beatgrid=_depends_on_for(grid_a)), db_path=db)
    grid_b = _beatgrid_record(stable_id, backend_version="1.7.0", decode_fingerprint=FINGERPRINT_V1)
    conn = open_conn(db)
    try:
        upsert_record(grid_b, conn=conn, cascade=False, version_bump=False, publish_live_event=False)
        assert canonical_pointer(conn, stable_id, "key") is None
        if cascade:
            outcomes = cascade_dependents(conn, stable_id=stable_id, lane="beatgrid", dependency_record=grid_b)
            assert [o.lane for o in outcomes if o.requeued] == ["key"]
        conn.commit()
    finally:
        conn.close()


def test_cascade_after_the_grid_write_marks_the_old_key_stale(state_db: Path) -> None:  # noqa: F811
    _key_on_regridded_track(state_db, "t-order", cascade=True)
    conn = open_conn(state_db)
    try:
        assert queue_store.stale_rows(conn, "t-order", "key") == {(BACKEND_NAME, PRODUCER_VERSION)}
        assert not _already_current(conn, _key_item("t-order"), BACKEND_NAME, PRODUCER_VERSION)
    finally:
        conn.close()


def test_an_unmarked_dependency_stale_key_is_not_current(state_db: Path) -> None:  # noqa: F811
    _key_on_regridded_track(state_db, "t-unmarked", cascade=False)
    conn = open_conn(state_db)
    try:
        assert queue_store.stale_rows(conn, "t-unmarked", "key") == set()
        assert not _already_current(conn, _key_item("t-unmarked"), BACKEND_NAME, PRODUCER_VERSION)
    finally:
        conn.close()


def test_a_key_matching_the_canonical_grid_is_still_current(state_db: Path) -> None:  # noqa: F811
    grid = _beatgrid_record("t-ok", backend_version="1.7.0", decode_fingerprint=FINGERPRINT_V1)
    upsert_record(grid, db_path=state_db)
    upsert_record(_key_record("t-ok", depends_on_beatgrid=_depends_on_for(grid)), db_path=state_db)
    conn = open_conn(state_db)
    try:
        assert _already_current(conn, _key_item("t-ok"), BACKEND_NAME, PRODUCER_VERSION)
    finally:
        conn.close()


def test_the_drain_done_set_omits_a_stale_key_and_keeps_a_current_one(state_db: Path) -> None:  # noqa: F811
    _key_on_regridded_track(state_db, "t-stale", cascade=False)
    grid = _beatgrid_record("t-fresh", backend_version="1.7.0", decode_fingerprint=FINGERPRINT_V1)
    upsert_record(grid, db_path=state_db)
    upsert_record(_key_record("t-fresh", depends_on_beatgrid=_depends_on_for(grid)), db_path=state_db)
    assert done_ids(lambda: open_conn(state_db), BACKEND_NAME) == {"t-fresh"}
