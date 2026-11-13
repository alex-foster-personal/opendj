"""Resume replan and duration-band sizing (NATIVE-10 / NATIVE-32).

[if] a backfill is cancelled and restarted [then] it resumes and reruns idempotently, [else stop].
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from apps.analysis import admission, queue_store
from apps.analysis import queue as queue_api
from apps.analysis.queue_runner import (
    BackendUnavailable,
    _abandon_failed_startup,
    _commit_record,
    run_batch,
)
from apps.analysis.store import open_conn
from apps.analysis.worker_sizing import HostCapacity, size_workers_for_host

from .queue_probe_backends import (
    PROBE_DB_ENV,
    PROBE_DIR_ENV,
    PROBE_SLEEP_ENV,
    BeatgridProbeV1,
    UnavailableBeatgridProbe,
)
from tests.analysis.queue_resume_support import (
    AMPLE_HOST,
    REPO_ROOT,
    _candidates,
    _kill_group,
    _marker_counts,
    _spawn_runner,
    probe_dir,
)

pytestmark = pytest.mark.requirement("NATIVE-10")


def test_resume_recomputes_a_missing_peak_instead_of_the_flat_floor(
    tmp_path: Path,
) -> None:
    """A nulled long-track peak must not be sized at the 650 MB floor.

    [if] a resumed track has no stored peak [then] RAM width uses the recomputed peak, [else stop].
    """
    conn = open_conn(tmp_path / "state.db")
    result = queue_api.enqueue(
        conn,
        _candidates(1, tmp_path, minutes=19.0),
        host=AMPLE_HOST,
    )
    conn.execute(
        "UPDATE analysis_queue_item SET predicted_peak_mb = NULL WHERE batch_id = ?",
        (result.batch_id,),
    )
    queue_api.resume(
        conn,
        result.batch_id,
        host=HostCapacity(available_mb=3000.0, cpu_count=8),
        per_worker_budget_mb=650.0,
    )
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    assert batch.workers == 1
    conn.close()


def test_resume_recomputes_a_non_finite_peak_instead_of_the_flat_floor(
    tmp_path: Path,
) -> None:
    """An infinite stored peak must not be sized at the 650 MB floor.

    [if] a resumed peak is not finite [then] RAM width uses the recomputed peak, [else stop].
    """
    conn = open_conn(tmp_path / "state.db")
    result = queue_api.enqueue(
        conn,
        _candidates(1, tmp_path, minutes=19.0),
        host=AMPLE_HOST,
    )
    conn.execute(
        "UPDATE analysis_queue_item SET predicted_peak_mb = ? WHERE batch_id = ?",
        (float("inf"), result.batch_id),
    )
    queue_api.resume(
        conn,
        result.batch_id,
        host=HostCapacity(available_mb=3000.0, cpu_count=8),
        per_worker_budget_mb=650.0,
    )
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    assert batch.workers == 1
    conn.close()


def test_resume_rejects_a_mixed_duration_set_before_touching_the_batch(
    tmp_path: Path,
) -> None:
    """A short known row beside an unknown one must not widen the batch.

    [if] a resumed batch mixes usable and unusable durations [then] resume raises and changes nothing, [else stop].
    """
    conn = open_conn(tmp_path / "state.db")
    candidates = _candidates(1, tmp_path, minutes=50.0) + _candidates(2, tmp_path, minutes=3.0)[1:]
    result = queue_api.enqueue(conn, candidates, host=AMPLE_HOST)
    planned = queue_store.get_batch(conn, result.batch_id)
    assert planned is not None
    assert planned.band == admission.BAND_OVER_45_MIN
    queue_api.cancel(conn, result.batch_id)
    conn.execute(
        "UPDATE analysis_queue_item SET duration_s = NULL WHERE batch_id = ? AND stable_id = ?",
        (result.batch_id, "sid_0"),
    )
    with pytest.raises(queue_api.QueueError, match="1 unusable of 2"):
        queue_api.resume(conn, result.batch_id, host=AMPLE_HOST)
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    assert batch.state == queue_store.BATCH_CANCELLED
    assert (batch.workers, batch.band) == (planned.workers, planned.band)
    states = {item.state for item in queue_store.list_items(conn, result.batch_id, limit=None)}
    assert states == {queue_store.ITEM_CANCELLED}
    conn.close()


@pytest.mark.requirement("NATIVE-32")
@pytest.mark.parametrize(
    "unusable_duration_s",
    [None, 0.0, float("nan")],
    ids=["null", "zero", "nan"],
)
def test_resume_replans_from_stored_band_when_every_duration_is_unusable(
    tmp_path: Path,
    unusable_duration_s: float | None,
) -> None:
    """Issue #6064: unusable durations must not collapse resume to zero workers.

    [if] all resumed durations are unusable [then] the stored band sizes workers, [else stop].
    """
    conn = open_conn(tmp_path / "state.db")
    result = queue_api.enqueue(
        conn,
        _candidates(1, tmp_path, minutes=50.0),
        host=AMPLE_HOST,
    )
    planned = queue_store.get_batch(conn, result.batch_id)
    assert planned is not None
    assert planned.band == admission.BAND_OVER_45_MIN
    queue_api.cancel(conn, result.batch_id)
    conn.execute(
        "UPDATE analysis_queue_item SET duration_s = ? WHERE batch_id = ?",
        (unusable_duration_s, result.batch_id),
    )
    conn.execute(
        "UPDATE analysis_queue_batch SET workers = 4 WHERE batch_id = ?",
        (result.batch_id,),
    )
    revived = queue_api.resume(conn, result.batch_id, host=AMPLE_HOST)
    assert revived == 1
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    assert batch.state == queue_store.BATCH_QUEUED
    assert (batch.workers, batch.band) == (1, admission.BAND_OVER_45_MIN)
    items = queue_store.list_items(conn, result.batch_id, limit=None)
    assert len(items) == 1
    assert items[0].state == queue_store.ITEM_PENDING
    conn.close()


@pytest.mark.requirement("NATIVE-32")
def test_resume_sizes_from_the_stored_band_when_enqueue_refused_the_unusable_durations(
    tmp_path: Path,
) -> None:
    """Issue #6064, through the production write path only.

    [if] a batch is enqueued with a long track plus NULL and zero durations, then
    cancelled and resumed [then] enqueue has already refused the unusable rows, resume
    revives only the long track, and the stored band still sizes one worker, never
    workers=0/band=empty, [else stop].

    The all-unusable runnable state the issue measured is UNAVAILABLE as evidence: no
    write path produces it. This head's enqueue refuses NULL and zero durations, and so
    did the pre-#5964 enqueue at baf4456f2f (checked by running that commit's
    `queue.enqueue`), so the old test could only reach it by rewriting rows by hand.
    """
    conn = open_conn(tmp_path / "state.db")
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\0")
    candidates = [
        admission.Candidate(
            stable_id=sid,
            lane="beatgrid",
            backend="own_beatgrid.backfill",
            file_path=str(audio),
            duration_s=duration_s,
        )
        for sid, duration_s in (("long", 50.0 * 60.0), ("null", None), ("zero", 0.0))
    ]
    result = queue_api.enqueue(conn, candidates, host=AMPLE_HOST)
    states = {i.stable_id: i.state for i in queue_store.list_items(conn, result.batch_id, limit=None)}
    assert states == {
        "long": queue_store.ITEM_PENDING,
        "null": queue_store.ITEM_REFUSED,
        "zero": queue_store.ITEM_REFUSED,
    }
    queue_api.cancel(conn, result.batch_id)
    assert queue_api.resume(conn, result.batch_id, host=AMPLE_HOST) == 1
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    assert batch.state == queue_store.BATCH_QUEUED
    assert (batch.workers, batch.band) == (1, admission.BAND_OVER_45_MIN)
    states = {i.stable_id: i.state for i in queue_store.list_items(conn, result.batch_id, limit=None)}
    assert states["long"] == queue_store.ITEM_PENDING
    assert {states["null"], states["zero"]} == {queue_store.ITEM_REFUSED}
    conn.close()


def test_resume_rejects_blank_model_provenance_when_every_peak_is_usable(
    tmp_path: Path,
) -> None:
    """Valid stored peaks do not excuse a model with no provenance.

    [if] a resumed batch's stored model has blank provenance [then] resume raises even with usable peaks, [else stop].
    """
    conn = open_conn(tmp_path / "state.db")
    result = queue_api.enqueue(conn, _candidates(2, tmp_path), host=AMPLE_HOST)
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    # Positive control: the untouched stored model resumes cleanly.
    queue_api.resume(conn, result.batch_id, host=AMPLE_HOST)
    conn.execute(
        "UPDATE analysis_queue_batch SET memory_model = ? WHERE batch_id = ?",
        (json.dumps({**batch.memory_model, "source": " "}), result.batch_id),
    )
    with pytest.raises(queue_api.QueueError, match="field source"):
        queue_api.resume(conn, result.batch_id, host=AMPLE_HOST)
    conn.close()
