"""Queue runner stops when jit warm-up is refused (issue #7227)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.analysis import admission
from apps.analysis import queue as queue_api
from apps.analysis.jit_warmup import WarmupResult
from apps.analysis.queue_runner import run_batch
from apps.analysis.store import open_conn
from tests.analysis.queue_probe_backends import BeatgridProbeV1


def _candidate(stable_id: str, tmp_path: Path) -> admission.Candidate:
    audio = tmp_path / f"{stable_id}.wav"
    audio.write_bytes(b"\0")
    return admission.Candidate(
        stable_id=stable_id,
        lane="beatgrid",
        backend=BeatgridProbeV1.name,
        file_path=str(audio),
        duration_s=180.0,
    )


@pytest.mark.requirement("JIT-8573")
def test_run_batch_raises_when_warm_up_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] warm-up is refused [then] run_batch raises before the pool, [else stop]."""
    from apps.analysis import queue_runner

    db = tmp_path / "state.db"
    conn = open_conn(db)
    result = queue_api.enqueue(conn, [_candidate("a", tmp_path)])
    refused = WarmupResult(
        backend=BeatgridProbeV1.name,
        seconds=0.0,
        lock_path=tmp_path / "x.lock",
        lock_held=False,
        waited_s=0.0,
        detail="lock refused",
        refused=True,
    )
    monkeypatch.setattr(queue_runner, "warm_backend_jit", lambda *a, **k: refused)

    def _must_not_spawn(_workers: int):
        raise AssertionError("spawn_pool must not run after a refused warm-up")

    monkeypatch.setattr(queue_runner, "spawn_pool", _must_not_spawn)

    with pytest.raises(RuntimeError, match="warm-up refused"):
        run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1)

    batch = conn.execute(
        "SELECT state, active_runner_id FROM analysis_queue_batch WHERE batch_id = ?",
        (result.batch_id,),
    ).fetchone()
    assert batch is not None
    assert batch[0] != "running" or batch[1] is None
