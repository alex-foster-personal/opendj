"""Producer version bumps and the dependency cascade re-queue the right set.

Lane brief `specs/native-analysis-v1-lanes/nav1-queue.md` item 1, requirement
NATIVE-10 ("[if] a producer version is bumped [then] the affected lanes are
re-queued rather than left on the old output").

Two mechanisms, deliberately separate:

* the VERSION BUMP re-queues tracks whose record for a producer is on an old
  version, and no track already on the new one;
* the DEPENDENCY CASCADE re-queues a dependent lane when the lane it depends
  on changes. It has two halves, and the brief is explicit that a design with
  only the record-level half is wrong: the record-level five-field
  ``depends_on`` block decides whether a SPECIFIC record is stale, but a key
  record that is ``missing`` with reason ``no_own_downbeats`` has no
  dependency identity at all, so only the STATIC lane edge can rescue it when
  the first canonical beatgrid appears.

Single-line intent, one assertion block each:
- if a track already on the new producer version is re-queued, a bump costs a
  full library re-analysis instead of the stale subset -- broken.
- if a track on the old version is not re-queued, the bump leaves it on old
  output, which is the exact NATIVE-10 line -- broken.
- if a key record whose depends_on names a superseded beatgrid stays
  canonical, the deck reads a key computed against a grid it is not showing
  -- broken.
- if a key that ran before any beatgrid existed is never re-queued once one
  appears, the static edge does not exist and the lane is stuck -- broken.
- if a key record whose depends_on already matches is re-queued anyway, the
  cascade re-analyzes the whole library on every beatgrid write -- broken.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.analysis import admission, queue_store
from apps.analysis import queue as queue_api
from apps.analysis.canonical import canonical_pointer
from apps.analysis.depends_on import declared_dependency, dependency_identity
from apps.analysis.queue_runner import drain, run_batch
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import open_conn

from .queue_probe_backends import (
    PROBE_DB_ENV,
    PROBE_DIR_ENV,
    BeatgridProbeV1,
    BeatgridProbeV2,
    KeyProbeV1,
)

pytestmark = pytest.mark.requirement("NATIVE-10")


@pytest.fixture()
def probe_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setenv(PROBE_DIR_ENV, str(markers))
    monkeypatch.setenv(PROBE_DB_ENV, str(tmp_path / "state.db"))
    return markers


def _cands(ids: list[str], tmp_path: Path, *, lane: str, backend: str) -> list:
    out = []
    for sid in ids:
        audio = tmp_path / f"{sid}.wav"
        audio.write_bytes(b"\0")
        out.append(
            admission.Candidate(
                stable_id=sid,
                lane=lane,
                backend=backend,
                file_path=str(audio),
                duration_s=200.0,
            )
        )
    return out


def _beatgrid_cands(ids: list[str], tmp_path: Path) -> list:
    return _cands(ids, tmp_path, lane="beatgrid", backend=BeatgridProbeV1.name)


def _key_cands(ids: list[str], tmp_path: Path) -> list:
    return _cands(ids, tmp_path, lane="key", backend=KeyProbeV1.name)


# --- producer version bump ------------------------------------------------

def test_a_version_bump_requeues_only_the_tracks_on_the_old_version(
    tmp_path: Path, probe_env: Path
) -> None:
    conn = open_conn(tmp_path / "state.db")
    first = queue_api.enqueue(conn, _beatgrid_cands(["a", "b", "c"], tmp_path))
    run_batch(conn, first.batch_id, backend_cls=BeatgridProbeV1)

    # Move ONE track forward on its own, so the bump has a genuine mix.
    ahead = queue_api.enqueue(conn, _beatgrid_cands(["c"], tmp_path))
    run_batch(conn, ahead.batch_id, backend_cls=BeatgridProbeV2)

    stale = queue_api.tracks_on_old_version(
        conn, backend=BeatgridProbeV1.name, current_version=BeatgridProbeV2.version
    )
    assert stale == ["a", "b"], "c already has a 2.0.0 row and must not re-queue"

    def resolve(conn_, ids, *, lane, backend):
        return _cands(list(ids), tmp_path, lane=lane, backend=backend)

    result = queue_api.requeue_for_version_bump(
        conn,
        backend=BeatgridProbeV1.name,
        current_version=BeatgridProbeV2.version,
        lane="beatgrid",
        resolve=resolve,
    )
    assert result is not None
    assert result.admitted == 2
    queued = {
        i.stable_id for i in queue_store.list_items(conn, result.batch_id)
    }
    assert queued == {"a", "b"}

    run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV2)
    for sid in ("a", "b", "c"):
        assert canonical_pointer(conn, sid, "beatgrid") == (
            BeatgridProbeV2.name,
            BeatgridProbeV2.version,
        )
    # Rows never overwrite across versions: the 1.0.0 rows are still there.
    assert conn.execute(
        "SELECT COUNT(*) FROM analysis WHERE backend_version = '1.0.0'"
    ).fetchone()[0] == 3
    conn.close()


def test_nothing_stale_returns_no_batch_rather_than_an_empty_one(
    tmp_path: Path, probe_env: Path
) -> None:
    conn = open_conn(tmp_path / "state.db")
    first = queue_api.enqueue(conn, _beatgrid_cands(["a"], tmp_path))
    run_batch(conn, first.batch_id, backend_cls=BeatgridProbeV1)

    def resolve(conn_, ids, *, lane, backend):  # pragma: no cover - not reached
        raise AssertionError("resolver must not run when nothing is stale")

    assert (
        queue_api.requeue_for_version_bump(
            conn,
            backend=BeatgridProbeV1.name,
            current_version=BeatgridProbeV1.version,
            lane="beatgrid",
            resolve=resolve,
        )
        is None
    )
    conn.close()


# --- dependency cascade ---------------------------------------------------

def test_key_enqueued_before_beatgrid_is_requeued_through_the_static_edge(
    tmp_path: Path, probe_env: Path
) -> None:
    """The brief's named regression test, end to end.

    Enqueue key BEFORE beatgrid on one track, run both, and assert the key
    lane is re-queued and ends ``ok`` with a ``depends_on`` block matching the
    new canonical beatgrid.
    """
    db = tmp_path / "state.db"
    conn = open_conn(db)

    key_first = queue_api.enqueue(conn, _key_cands(["a"], tmp_path))
    run_batch(conn, key_first.batch_id, backend_cls=KeyProbeV1)
    # It ran before any beatgrid existed, so it has no dependency identity.
    row = conn.execute(
        "SELECT record_json FROM analysis WHERE stable_id='a' AND backend=?",
        (KeyProbeV1.name,),
    ).fetchone()
    early = AnalysisRecord.from_json(row[0])
    assert early.lanes["key"].status == "missing"
    assert early.lanes["key"].reason == queue_api.NO_OWN_DOWNBEATS
    assert declared_dependency(early, "beatgrid") is None

    def resolve(conn_, ids, *, lane):
        return _cands(
            list(ids), tmp_path, lane=lane, backend=KeyProbeV1.name
        )

    grid = queue_api.enqueue(conn, _beatgrid_cands(["a"], tmp_path))
    summaries = drain(
        conn,
        grid.batch_id,
        backend_for_lane=lambda lane: (
            BeatgridProbeV1 if lane == "beatgrid" else KeyProbeV1
        ),
        cascade_resolver=resolve,
    )
    # Two batches ran: the beatgrid one, and the key one its cascade created.
    assert len(summaries) == 2, [s.batch_id for s in summaries]
    assert summaries[0].cascade_batch_id == summaries[1].batch_id
    assert summaries[1].completed == 1

    grid_record = AnalysisRecord.from_json(
        conn.execute(
            "SELECT record_json FROM analysis WHERE stable_id='a' AND backend=?",
            (BeatgridProbeV1.name,),
        ).fetchone()[0]
    )
    key_record = AnalysisRecord.from_json(
        conn.execute(
            "SELECT record_json FROM analysis WHERE stable_id='a' AND backend=?",
            (KeyProbeV1.name,),
        ).fetchone()[0]
    )
    assert key_record.lanes["key"].status == "ok"
    assert declared_dependency(key_record, "beatgrid") == dependency_identity(
        grid_record
    )
    assert canonical_pointer(conn, "a", "key") == (
        KeyProbeV1.name, KeyProbeV1.version
    )
    conn.close()


def test_a_beatgrid_bump_makes_the_old_key_stale_and_non_canonical(
    tmp_path: Path, probe_env: Path
) -> None:
    db = tmp_path / "state.db"
    conn = open_conn(db)

    def resolve(conn_, ids, *, lane):
        return _cands(list(ids), tmp_path, lane=lane, backend=KeyProbeV1.name)

    grid = queue_api.enqueue(conn, _beatgrid_cands(["a"], tmp_path))
    drain(
        conn,
        grid.batch_id,
        backend_for_lane=lambda lane: (
            BeatgridProbeV1 if lane == "beatgrid" else KeyProbeV1
        ),
        cascade_resolver=resolve,
    )
    assert canonical_pointer(conn, "a", "key") == (
        KeyProbeV1.name, KeyProbeV1.version
    )

    # Now bump the beatgrid producer. The key record's depends_on names the
    # 1.0.0 grid, so it must go stale AND stop being canonical.
    bumped = queue_api.enqueue(conn, _beatgrid_cands(["a"], tmp_path))
    summary = run_batch(conn, bumped.batch_id, backend_cls=BeatgridProbeV2)
    requeued = [o for o in summary.cascade_outcomes if o.requeued]
    assert [o.lane for o in requeued] == ["key"]
    assert queue_store.stale_rows(conn, "a", "key") == {
        (KeyProbeV1.name, KeyProbeV1.version)
    }
    assert canonical_pointer(conn, "a", "key") is None, (
        "a key computed against a superseded beatgrid must not stay canonical"
    )
    # The row itself is untouched: staleness excludes it from the pointer,
    # it does not delete produced work.
    assert conn.execute(
        "SELECT COUNT(*) FROM analysis WHERE stable_id='a' AND backend=?",
        (KeyProbeV1.name,),
    ).fetchone()[0] == 1
    conn.close()


def test_a_matching_depends_on_block_is_not_requeued(
    tmp_path: Path, probe_env: Path
) -> None:
    """The control that stops the cascade re-analyzing everything forever."""
    db = tmp_path / "state.db"
    conn = open_conn(db)

    def resolve(conn_, ids, *, lane):
        return _cands(list(ids), tmp_path, lane=lane, backend=KeyProbeV1.name)

    grid = queue_api.enqueue(conn, _beatgrid_cands(["a"], tmp_path))
    drain(
        conn,
        grid.batch_id,
        backend_for_lane=lambda lane: (
            BeatgridProbeV1 if lane == "beatgrid" else KeyProbeV1
        ),
        cascade_resolver=resolve,
    )

    # Re-run the SAME beatgrid producer at the SAME version. The canonical
    # pointer does not move, the key's depends_on still matches, so nothing
    # is re-queued.
    again = queue_api.enqueue(conn, _beatgrid_cands(["a"], tmp_path))
    summary = run_batch(conn, again.batch_id, backend_cls=BeatgridProbeV1)
    assert summary.skipped == 1, "the grid was already current, so it re-ran nothing"
    assert [o for o in summary.cascade_outcomes if o.requeued] == []
    assert queue_store.stale_rows(conn, "a", "key") == set()
    conn.close()
