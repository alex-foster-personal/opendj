"""``_resolve_tracks`` must exclude soft-deleted rows (issue #2410 item 4).

[if] a soft-deleted stable_id is requested [then] it is reported as an
unknown/missing stable_id (StageBlocked), never silently resolved or
silently skipped [else broken].
[if] a live stable_id alongside a soft-deleted one is requested [then] only
the soft-deleted one is named in the refusal [else broken].
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.lyrics.batch import BatchPaths, BatchReport, StageBlocked, _resolve_tracks

NOW = "2026-09-01T00:00:00+00:00"


def _seed_track(conn, stable_id: str, *, deleted: bool) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, file_path, "
        "created_at, updated_at, deleted_at) VALUES (?, 'inferred', ?, ?, "
        "?, ?, ?)",
        (stable_id, stable_id, f"/music/{stable_id}.flac", NOW, NOW,
         NOW if deleted else None),
    )
    conn.commit()


def test_a_soft_deleted_track_is_reported_missing_not_resolved(
    data_dir: Path, conn,
) -> None:
    _seed_track(conn, "trk-live", deleted=False)
    _seed_track(conn, "trk-gone", deleted=True)
    paths = BatchPaths(state_db=data_dir / "state" / "state.db")
    report = BatchReport(corpus="tbatch", live=False)
    with pytest.raises(StageBlocked, match="trk-gone") as caught:
        _resolve_tracks("tbatch", ["trk-live", "trk-gone"], paths, report)
    assert "trk-live" not in str(caught.value)


def test_a_live_track_alone_still_resolves(data_dir: Path, conn) -> None:
    _seed_track(conn, "trk-live", deleted=False)
    paths = BatchPaths(state_db=data_dir / "state" / "state.db")
    report = BatchReport(corpus="tbatch", live=False)
    _resolve_tracks("tbatch", ["trk-live"], paths, report)
    assert [plan.stable_id for plan in report.tracks] == ["trk-live"]
