"""Real queue-child invocation coverage for the daemon's analysis lanes (#8739).

[if] the daemon invokes a queue child [then] its result remains actionable, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.analysis import queue as queue_api
from apps.analysis import queue_store, queue_targets
from apps.analysis.depends_on import DEPENDS_ON_KEY, dependency_identity
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import open_conn, upsert_record
from apps.webui.server import ahead_analysis as aa
from apps.webui.server import ingest_enrichment
from tests.analysis.queue_probe_backends import (
    _base_record,
    _beatgrid_payload,
    _key_payload,
)

pytestmark = pytest.mark.requirement("NATIVE-10")


class LoudnessInvocationProbe:
    """Model-free loudness backend importable by spawned queue workers."""

    name = "own_loudness.backfill"
    version = "8739.1.0"

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        return "queue invocation probe has no JIT cache"

    @classmethod
    def analyze(cls, _path: Path, stable_id: str) -> AnalysisRecord:
        return _base_record(
            stable_id,
            cls.name,
            cls.version,
            "loudness",
            LaneResult(
                status="ok",
                confidence=0.9,
                payload={
                    "integrated_lufs": -12.0,
                    "true_peak_dbtp": -1.0,
                    "loudness_range_lu": 4.0,
                    "rms_db": -15.0,
                },
            ),
        )


class BeatgridInvocationProbe:
    """Model-free beatgrid backend importable by spawned queue workers."""

    name = "own_beatgrid.backfill"
    version = "8739.1.0"

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        return ()

    @classmethod
    def warm_jit_cache(cls) -> str:
        return "queue invocation probe has no JIT cache"

    @classmethod
    def analyze(cls, _path: Path, stable_id: str) -> AnalysisRecord:
        return _base_record(
            stable_id,
            cls.name,
            cls.version,
            "beatgrid",
            LaneResult(status="ok", confidence=0.9, payload=_beatgrid_payload()),
        )


def _seed_batch(
    tmp_path: Path,
    *,
    lane: str,
    backend_cls: type[LoudnessInvocationProbe] | type[BeatgridInvocationProbe],
) -> tuple[Path, str]:
    db = tmp_path / f"{lane}.db"
    audio = tmp_path / f"{lane}.wav"
    audio.write_bytes(b"\0")
    stable_id = f"{lane}-track"
    conn = open_conn(db)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
        "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, 180000, ?, "
        "'2026-11-06T00:00:00Z', '2026-11-06T00:00:00Z')",
        (stable_id, stable_id, str(audio)),
    )
    conn.commit()
    if lane == "beatgrid":
        future_grid = BeatgridInvocationProbe.analyze(audio, stable_id)
        upsert_record(
            _base_record(
                stable_id,
                "own_key.backfill",
                "8739.1.0",
                "key",
                LaneResult(status="ok", confidence=0.8, payload=_key_payload()),
                features={DEPENDS_ON_KEY: {"beatgrid": dependency_identity(future_grid)}},
            ),
            conn=conn,
            cascade=False,
            version_bump=False,
            publish_live_event=False,
        )
    candidates = queue_targets.candidates_from_state(
        conn,
        [stable_id],
        lane=lane,
        backend=backend_cls.name,
    )
    batch = queue_api.enqueue(conn, candidates, note="issue-8739-invocation")
    conn.close()
    assert batch.admitted == 1
    return db, batch.batch_id


@pytest.mark.parametrize(
    ("lane", "backend_cls"),
    [
        ("loudness", LoudnessInvocationProbe),
        ("beatgrid", BeatgridInvocationProbe),
    ],
)
def test_e2e_host_queue_invocation_exits_zero(
    tmp_path: Path,
    lane: str,
    backend_cls: type[LoudnessInvocationProbe] | type[BeatgridInvocationProbe],
) -> None:
    """[if] the host runs either lane [then] its real queue child exits zero, [else stop]."""
    db, batch_id = _seed_batch(tmp_path, lane=lane, backend_cls=backend_cls)
    backend_spec = f"{__name__}:{backend_cls.__name__}"

    code, out, err = aa.run_queued_batch(str(db), batch_id, backend_spec)

    report = json.loads(out)
    assert code == 0
    assert report["batch_id"] == batch_id
    assert report["completed"] == 1
    assert "chose " in err and " worker(s)" in err
    assert "chose " not in out
    conn = open_conn(db)
    [item] = queue_store.list_items(conn, batch_id)
    conn.close()
    assert item.state == queue_store.ITEM_DONE


def test_argparse_failure_surfaces_in_lane_unavailable(tmp_path: Path) -> None:
    """[if] argparse rejects a flag [then] stderr reaches LANE_UNAVAILABLE, [else stop]."""
    invalid_flag = "--issue-8739-invalid-flag"
    code, out, err = aa._queue_cli(
        [
            "run",
            "--batch-id",
            "missing-batch",
            "--backend",
            LoudnessInvocationProbe.name,
            invalid_flag,
        ],
        str(tmp_path / "state.db"),
    )

    assert code == 2
    assert out == ""
    assert invalid_flag in err
    reason = f"queue run exit {code}: {aa._last_line(aa._failure_text(out, err))}"
    stable_ids = ["track-a", "track-b"]
    drain = aa.AheadDrain(
        aa.AheadSources(
            present_fn=lambda: stable_ids,
            mapped_fn=set,
            has_strip_fn=lambda _sid: True,
            write_strip_fn=lambda _sid: None,
            done_fn=lambda _lane, _backend: set(),
            run_lane_fn=lambda _lane, _backend, ids: dict.fromkeys(ids, reason),
            playing_fn=lambda: False,
            blank_tags_fn=set,
            refresh_tags_fn=lambda _sid: True,
            declined_fn=lambda _lane, _backend: {},
        )
    )
    drain._run_lane("loudness", LoudnessInvocationProbe.name, stable_ids)

    warnings = ingest_enrichment.capability_warnings(
        drain_running=True,
        unavailable=drain.status().unavailable,
        artwork_online=True,
        ai_missing_keys=[],
    )
    [warning] = warnings
    assert warning.code == "LANE_UNAVAILABLE"
    assert invalid_flag in warning.message
    assert "chose " not in warning.message
