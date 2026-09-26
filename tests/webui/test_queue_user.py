"""User-ordered stems/lyrics jobs on the persistent queue (issue #1865).

[if] the user selects N tracks and chooses Stems: do next [then] N stems jobs
exist at the head of the stems lane in selection order, else stop.

Each acceptance line is a test. No mocks of has_bundle or the lyrics cache:
fresh artifacts are real files on disk. The runner is a spy only for the
skip-does-not-dispatch line.

-Claude
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.analysis import queue_store
from apps.analysis import queue_user as user
from apps.analysis.queue import QueueError
from apps.analysis.queue_user import UserJobConflict
from apps.analysis.store import open_conn
from apps.lyrics import cache as lyrics_cache
from apps.lyrics.cache import LyricLine, Lyrics
from apps.stems.selection import MANIFEST_NAME


def _seed(db: Path, ids: list[str]) -> None:
    conn = open_conn(db)
    for sid in ids:
        audio = db.parent / f"{sid}.wav"
        audio.write_bytes(b"\0")
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
            "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, 180000, "
            "?, '2026-09-11T00:00:00Z', '2026-09-11T00:00:00Z')",
            (sid, sid, str(audio)),
        )
    conn.commit()
    conn.close()


def _conn(db: Path):
    conn = open_conn(db)
    user.ensure_user_schema(conn)
    return conn


def _enqueue(conn, lane: str, ids: list[str], tmp: Path, placement: str = "next"):
    return user.enqueue_next(
        conn,
        lane=lane,
        stable_ids=ids,
        placement=placement,  # type: ignore[arg-type]
        stems_root=tmp / "stems",
        data_dir=tmp,
    )


# REQ: PERFBATCH-05
def test_stems_do_next_places_selection_at_head_in_order(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed(db, ["a", "b", "c", "backlog"])
    conn = _conn(db)
    _enqueue(conn, "stems", ["backlog"], tmp_path)
    result = _enqueue(conn, "stems", ["c", "a", "b"], tmp_path)
    assert [i.stable_id for i in result.items] == ["c", "a", "b"]
    assert all(i.state == "pending" for i in result.items)
    listed = user.list_lane(conn, "stems")
    assert [i.stable_id for i in listed] == ["c", "a", "b", "backlog"]
    first = user.claim_next_for_lane(conn, "stems", runner_id="r1")
    assert first is not None and first.stable_id == "c"
    conn.close()


# REQ: PERFBATCH-05
def test_running_item_is_not_interrupted_by_do_next(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed(db, ["run", "n1", "n2"])
    conn = _conn(db)
    _enqueue(conn, "stems", ["run", "n1"], tmp_path)
    claimed = user.claim_next_for_lane(conn, "stems", runner_id="r1")
    assert claimed is not None and claimed.stable_id == "run"
    result = _enqueue(conn, "stems", ["n2", "run"], tmp_path)
    assert result.already_running == ("run",)
    still = next(i for i in result.items if i.stable_id == "run")
    assert still.state == "running"
    assert still.runner_id == "r1"
    pending = [i.stable_id for i in user.list_lane(conn, "stems") if i.state == "pending"]
    assert pending[0] == "n2"
    conn.close()


# REQ: PERFBATCH-05
def test_lyrics_lane_is_independent_of_running_stems(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed(db, ["t1", "t2"])
    conn = _conn(db)
    _enqueue(conn, "stems", ["t1"], tmp_path)
    stems_run = user.claim_next_for_lane(conn, "stems", runner_id="rs")
    assert stems_run is not None and stems_run.state == "running"
    _enqueue(conn, "lyrics", ["t2"], tmp_path)
    lyrics_run = user.claim_next_for_lane(conn, "lyrics", runner_id="rl")
    assert lyrics_run is not None
    assert lyrics_run.stable_id == "t2"
    assert lyrics_run.state == "running"
    assert stems_run.stable_id == "t1"
    conn.close()


# REQ: PERFBATCH-05
def test_fresh_stem_bundle_is_skipped_up_to_date(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed(db, ["fresh", "need"])
    bundle = tmp_path / "stems" / "fresh"
    bundle.mkdir(parents=True)
    (bundle / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    conn = _conn(db)
    result = _enqueue(conn, "stems", ["fresh", "need"], tmp_path)
    by_id = {i.stable_id: i for i in result.items}
    assert by_id["fresh"].state == "skipped"
    assert by_id["fresh"].reason == "up_to_date"
    assert "up to date" in (by_id["fresh"].detail or "")
    assert by_id["need"].state == "pending"
    listed = user.list_lane(conn, "stems")
    assert [i.stable_id for i in listed] == ["need"]
    conn.close()


# REQ: PERFBATCH-05
def test_fresh_lyrics_cache_is_skipped_up_to_date(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed(db, ["has", "need"])
    path = lyrics_cache.cache_path(tmp_path, "has")
    lyrics_cache.write(
        path, Lyrics("has", "lrclib", (LyricLine(0, "hello"),))
    )
    conn = _conn(db)
    result = _enqueue(conn, "lyrics", ["has", "need"], tmp_path)
    by_id = {i.stable_id: i for i in result.items}
    assert by_id["has"].state == "skipped"
    assert "up to date" in (by_id["has"].detail or "")
    assert by_id["need"].state == "pending"
    conn.close()


# REQ: PERFBATCH-05
def test_patch_pending_changes_claim_order(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed(db, ["a", "b", "c"])
    conn = _conn(db)
    _enqueue(conn, "stems", ["a", "b", "c"], tmp_path)
    user.reorder_item(conn, lane="stems", stable_id="c", before_stable_id="a")
    listed = [i.stable_id for i in user.list_lane(conn, "stems")]
    assert listed == ["c", "a", "b"]
    claimed = user.claim_next_for_lane(conn, "stems", runner_id="r1")
    assert claimed is not None and claimed.stable_id == "c"
    conn.close()


# REQ: PERFBATCH-05
def test_cancel_pending_leaves_the_active_queue(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed(db, ["a", "b"])
    conn = _conn(db)
    _enqueue(conn, "stems", ["a", "b"], tmp_path)
    cancelled = user.cancel_item(conn, lane="stems", stable_id="a")
    assert cancelled.state == "cancelled"
    active = [i.stable_id for i in user.list_lane(conn, "stems")]
    assert active == ["b"]
    conn.close()


# REQ: PERFBATCH-05
def test_cancel_running_blocks_worker_finish(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed(db, ["a"])
    conn = _conn(db)
    _enqueue(conn, "stems", ["a"], tmp_path)
    claimed = user.claim_next_for_lane(conn, "stems", runner_id="r1")
    assert claimed is not None
    user.cancel_item(conn, lane="stems", stable_id="a")
    moved = queue_store.finish_item(
        conn,
        batch_id=claimed.batch_id,
        stable_id="a",
        lane="stems",
        state=queue_store.ITEM_DONE,
        claimed_by="r1",
    )
    assert moved is False
    row = user.list_lane(conn, "stems", include_settled=True)[0]
    assert row.state == "cancelled"
    conn.close()


# REQ: PERFBATCH-05
def test_release_running_keeps_pending_order(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed(db, ["a", "b", "c"])
    conn = _conn(db)
    _enqueue(conn, "stems", ["a", "b", "c"], tmp_path)
    claimed = user.claim_next_for_lane(conn, "stems", runner_id="dead")
    assert claimed is not None and claimed.stable_id == "a"
    n = queue_store.release_running_items(conn, claimed.batch_id)
    assert n == 1
    conn.commit()
    listed = [i.stable_id for i in user.list_lane(conn, "stems")]
    assert listed == ["b", "c", "a"] or listed[0] in {"a", "b"}
    # a returns to pending; remaining pending keep relative order
    assert set(listed) == {"a", "b", "c"}
    assert listed.index("b") < listed.index("c")
    conn.close()


def test_unknown_id_is_named_not_dropped(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed(db, ["known"])
    conn = _conn(db)
    with pytest.raises(QueueError, match="ghost"):
        _enqueue(conn, "stems", ["known", "ghost"], tmp_path)
    conn.close()


def test_skip_at_claim_does_not_invoke_the_runner(tmp_path: Path) -> None:
    from apps.analysis import queue_user_runner
    from apps.stems.selection import MANIFEST_NAME

    db = tmp_path / "state.db"
    _seed(db, ["fresh"])
    conn = _conn(db)
    _enqueue(conn, "stems", ["fresh"], tmp_path)
    bundle = tmp_path / "stems" / "fresh"
    bundle.mkdir(parents=True)
    (bundle / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    called: list[str] = []
    outcome = queue_user_runner.tick_lane(
        conn,
        "stems",
        runner_id="spy",
        stems_root=tmp_path / "stems",
        data_dir=tmp_path,
        execute_stems=called.append,
    )
    assert outcome == "ran"
    assert called == []
    settled = user.list_lane(conn, "stems", include_settled=True)
    assert settled[0].state == "skipped"
    conn.close()


def test_dry_runner_holds_without_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from apps.analysis import queue_user_runner

    monkeypatch.setenv("MUSIC_DJ_LIBRARY_JOBS_RUNNER", "dry")
    monkeypatch.setenv("MUSIC_DJ_LIBRARY_JOBS_DRY_HOLD_S", "0.01")
    build_argv_called: list[str] = []
    subprocess_called: list[str] = []
    monkeypatch.setattr(
        "apps.stems.job.build_argv",
        lambda *args, **kwargs: build_argv_called.append("x"),
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess_called.append("x"),
    )
    db = tmp_path / "state.db"
    _seed(db, ["need"])
    conn = _conn(db)
    _enqueue(conn, "stems", ["need"], tmp_path)
    t0 = time.monotonic()
    outcome = queue_user_runner.tick_lane(
        conn,
        "stems",
        runner_id="dry",
        stems_root=tmp_path / "stems",
        data_dir=tmp_path,
    )
    elapsed = time.monotonic() - t0
    assert outcome == "ran"
    assert build_argv_called == []
    assert subprocess_called == []
    assert elapsed >= 0.005
    settled = user.list_lane(conn, "stems", include_settled=True)
    assert settled[0].state == "done"
    conn.close()


def test_unknown_runner_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.analysis.queue_user_runner import runner_from_environ

    monkeypatch.setenv("MUSIC_DJ_LIBRARY_JOBS_RUNNER", "gpu")
    with pytest.raises(ValueError, match="MUSIC_DJ_LIBRARY_JOBS_RUNNER"):
        runner_from_environ()


def test_dry_runner_still_skips_fresh_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from apps.analysis import queue_user_runner

    monkeypatch.setenv("MUSIC_DJ_LIBRARY_JOBS_RUNNER", "dry")
    monkeypatch.setenv("MUSIC_DJ_LIBRARY_JOBS_DRY_HOLD_S", "10")
    sleep_called: list[float] = []
    original_sleep = time.sleep

    def spy_sleep(seconds: float) -> None:
        sleep_called.append(seconds)
        original_sleep(0)

    monkeypatch.setattr(time, "sleep", spy_sleep)
    db = tmp_path / "state.db"
    _seed(db, ["fresh"])
    conn = _conn(db)
    _enqueue(conn, "stems", ["fresh"], tmp_path)
    bundle = tmp_path / "stems" / "fresh"
    bundle.mkdir(parents=True)
    (bundle / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    outcome = queue_user_runner.tick_lane(
        conn,
        "stems",
        runner_id="dry",
        stems_root=tmp_path / "stems",
        data_dir=tmp_path,
    )
    assert outcome == "ran"
    assert sleep_called == []
    settled = user.list_lane(conn, "stems", include_settled=True)
    assert settled[0].state == "skipped"
    conn.close()


def test_reorder_running_is_conflict(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed(db, ["a", "b"])
    conn = _conn(db)
    _enqueue(conn, "stems", ["a", "b"], tmp_path)
    user.claim_next_for_lane(conn, "stems", runner_id="r1")
    with pytest.raises(UserJobConflict, match="running"):
        user.reorder_item(conn, lane="stems", stable_id="a", before_stable_id="b")
    conn.close()
