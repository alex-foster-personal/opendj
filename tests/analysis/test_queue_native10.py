"""Remaining NATIVE-10 queue gaps: mid-batch progress, auto re-queue, lanes, AST.

Issue #2258. Closes the holes the #1586 audit left: ``progress()`` mid-run,
``auto_requeue_on_version_bump`` via ``upsert_record``, stems/lyrics exclusion,
backend registration, and a CI-scale no-network scan of v1 producer modules.

No mocks. Fail fast.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import types
from pathlib import Path

import pytest

from apps.analysis import admission, queue_store
from apps.analysis import queue as queue_api
from apps.analysis.backends import get_backend
from apps.analysis.lane_enums import LANES
from apps.analysis.queue import QueueError
from apps.analysis.queue_runner import run_batch
from apps.analysis.queue_user_lanes import USER_JOB_LANES
from apps.analysis.store import open_conn, upsert_record

from .queue_probe_backends import (
    PROBE_DIR_ENV,
    BeatgridProbeV1,
    BeatgridProbeV2,
)

pytestmark = pytest.mark.requirement("NATIVE-10")

_FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {"urlopen", "requests", "httpx", "urllib", "hub"}
)

_V1_AST_MODULES: tuple[str, ...] = (
    "apps.analysis.backends.own_key",
    "apps.analysis.backends.own_loudness",
    "apps.analysis.backends.own_waveform",
    "apps.analysis.backends.own_beatgrid",
    "apps.analysis_beatgrid.weights",
    "apps.analysis.admission",
)

_OWN_BACKFILL_BACKENDS: tuple[str, ...] = (
    "own_beatgrid.backfill",
    "own_key.backfill",
    "own_waveform.backfill",
    "own_loudness.backfill",
)

_V1_LANES: tuple[str, ...] = ("beatgrid", "key", "waveform", "loudness")


def _referenced_names(module: types.ModuleType) -> set[str]:
    """Every identifier the module's CODE references (docstrings excluded)."""
    tree = ast.parse(inspect.getsource(module))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.add(node.name)
    return out


def _torch_hub_referenced(module: types.ModuleType) -> bool:
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "hub"
            and isinstance(node.value, ast.Name)
            and node.value.id == "torch"
        ):
            return True
    return False


def _candidates(n: int, tmp_path: Path, *, minutes: float = 3.0) -> list:
    out = []
    for i in range(n):
        audio = tmp_path / f"t{i}.wav"
        audio.write_bytes(b"\0")
        out.append(
            admission.Candidate(
                stable_id=f"sid_{i}",
                lane="beatgrid",
                backend=BeatgridProbeV1.name,
                file_path=str(audio),
                duration_s=minutes * 60.0,
            )
        )
    return out


def _beatgrid_cands(ids: list[str], tmp_path: Path) -> list:
    out = []
    for sid in ids:
        audio = tmp_path / f"{sid}.wav"
        audio.write_bytes(b"\0")
        out.append(
            admission.Candidate(
                stable_id=sid,
                lane="beatgrid",
                backend=BeatgridProbeV1.name,
                file_path=str(audio),
                duration_s=200.0,
            )
        )
    return out


def _seed_library(db: Path, rows: list[tuple[str, float | None]]) -> None:
    conn = open_conn(db)
    for stable_id, minutes in rows:
        audio = db.parent / f"{stable_id}.wav"
        audio.write_bytes(b"\0")
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
            "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, "
            "'2026-09-09T00:00:00Z', '2026-09-09T00:00:00Z')",
            (
                stable_id,
                stable_id,
                int(minutes * 60_000) if minutes is not None else None,
                str(audio),
            ),
        )
    conn.commit()
    conn.close()


def _lane_candidate(
    tmp_path: Path, *, lane: str, backend: str, stable_id: str = "probe"
) -> admission.Candidate:
    audio = tmp_path / f"{stable_id}_{lane}.wav"
    audio.write_bytes(b"\0")
    return admission.Candidate(
        stable_id=stable_id,
        lane=lane,
        backend=backend,
        file_path=str(audio),
        duration_s=200.0,
    )


@pytest.fixture()
def probe_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "markers"
    d.mkdir()
    monkeypatch.setenv(PROBE_DIR_ENV, str(d))
    return d


def test_progress_reports_running_and_pending_mid_batch(
    tmp_path: Path, probe_dir: Path
) -> None:
    db = tmp_path / "state.db"
    conn = open_conn(db)
    result = queue_api.enqueue(conn, _candidates(3, tmp_path))
    assert result.admitted == 3

    item = queue_store.claim_next(conn, result.batch_id, runner_id="runner-a")
    assert item is not None
    assert item.state == queue_store.ITEM_RUNNING

    prog = queue_api.progress(conn, result.batch_id)
    assert prog.counts[queue_store.ITEM_RUNNING] == 1
    assert prog.counts[queue_store.ITEM_PENDING] == 2
    assert prog.counts.get(queue_store.ITEM_DONE, 0) == 0
    assert prog.state in queue_store.BATCH_STATES
    assert prog.batch_id == result.batch_id
    conn.close()


def test_upsert_of_a_new_producer_version_requeues_only_stale_tracks(
    tmp_path: Path, probe_dir: Path
) -> None:
    db = tmp_path / "state.db"
    _seed_library(db, [("a", 4.0), ("b", 4.0), ("c", 4.0)])
    conn = open_conn(db)
    first = queue_api.enqueue(conn, _beatgrid_cands(["a", "b", "c"], tmp_path))
    run_batch(conn, first.batch_id, backend_cls=BeatgridProbeV1)
    batches_before = queue_store.list_batches(conn)
    assert len(batches_before) == 1

    c_audio = tmp_path / "c.wav"
    v2_record = BeatgridProbeV2.analyze(c_audio, "c")
    upsert_record(v2_record, conn=conn)

    batches_after = queue_store.list_batches(conn)
    assert len(batches_after) == 2
    requeue_batch_id = batches_after[0].batch_id
    queued = {
        i.stable_id
        for i in queue_store.list_items(conn, requeue_batch_id)
    }
    assert queued == {"a", "b"}
    assert "c" not in queued

    upsert_record(v2_record, conn=conn)
    assert len(queue_store.list_batches(conn)) == 2
    conn.close()


def test_v1_enqueue_refuses_stems_and_lyrics(tmp_path: Path) -> None:
    assert USER_JOB_LANES
    assert LANES
    assert set(USER_JOB_LANES).isdisjoint(set(LANES))

    db = tmp_path / "state.db"
    conn = open_conn(db)
    for lane in USER_JOB_LANES:
        with pytest.raises(QueueError, match=lane):
            queue_api.enqueue(
                conn,
                [_lane_candidate(tmp_path, lane=lane, backend=f"own_{lane}.backfill")],
            )

    for lane, backend in zip(
        _V1_LANES,
        _OWN_BACKFILL_BACKENDS,
        strict=True,
    ):
        result = queue_api.enqueue(
            conn,
            [_lane_candidate(tmp_path, lane=lane, backend=backend, stable_id=lane)],
        )
        assert result.admitted == 1, lane
    conn.close()


def test_the_four_own_backfill_backends_are_registered() -> None:
    for name in _OWN_BACKFILL_BACKENDS:
        cls = get_backend(name)
        assert cls.name == name


def test_v1_own_backends_do_not_import_a_network_client() -> None:
    own_key = importlib.import_module("apps.analysis.backends.own_key")
    source = inspect.getsource(own_key)
    assert "chroma_cqt" in source or "REQUIRED_MODULES" in source

    for mod_name in _V1_AST_MODULES:
        module = importlib.import_module(mod_name)
        names = _referenced_names(module)
        hits = names & _FORBIDDEN_NAMES
        assert not hits, f"{mod_name} references forbidden names: {sorted(hits)}"
        assert not _torch_hub_referenced(module), (
            f"{mod_name} references torch.hub"
        )
