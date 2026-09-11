"""Key records with a stale `depends_on.beatgrid` are excluded from canonical.

nav1-key-record.md section 2: when the canonical beatgrid changes after a key
record was written, the key lane must read `missing` with `stale_dependency`
until recomputed. Both stale cases are covered here:

  (a) a newer beatgrid backend version replaces the old one, and
  (b) the same version triple but a different `decode_fingerprint`.

  [if] the canonical beatgrid changes after a key record was written [then]
  `/anlz` and the canonical pointer both treat the key lane as stale,
  [else stop]

-Claude
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis import selection
from apps.analysis.backends.own_key import record_from_estimate
from apps.analysis.canonical import canonical_pointer, recompute_canonical
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import open_conn, upsert_record
from apps.analysis_key import canon, flags, profiles
from apps.analysis_key.lane_payload import (
    REASON_STALE_DEPENDENCY,
    depends_on_identity,
)
from apps.webui.server.rb_vendor_pkg import own_key_overlay as overlay_mod

FINGERPRINT_V1 = "sha256:" + ("aa" * 32)
FINGERPRINT_V2 = "sha256:" + ("bb" * 32)
C_MAJOR = canon.Key(0, False)

_OK_SEGMENTS = {
    "status": "ok",
    "reason": None,
    "segments": [
        {
            "start_bar": 0,
            "end_bar": 7,
            "start_s": 0.0,
            "end_s": 16.0,
            "key_camelot": "8B",
            "key_openkey": "1d",
            "confidence": 0.9,
        }
    ],
}


def _beatgrid_record(
    stable_id: str,
    *,
    backend_version: str,
    decode_fingerprint: str,
    n_beats: int = 16,
) -> AnalysisRecord:
    beats = [
        {"t": round(index * 0.5, 5), "n": (index % 4) + 1, "bpm": 120.0}
        for index in range(n_beats)
    ]
    payload = {
        "beats": beats,
        "bpm": 120.0,
        "bpm_confidence": 0.9,
        "octave_reason": "in_band",
        "first_downbeat_s": 0.0,
        "tempo_changes": [],
        "static_grid_untrusted": False,
    }
    return AnalysisRecord(
        stable_id=stable_id,
        backend="own_beatgrid.backfill",
        backend_version=backend_version,
        analyzed_at=datetime.now(UTC),
        duration_s=8.0,
        sample_rate=44100,
        bpm=120.0,
        bpm_confidence=0.9,
        key_camelot="",
        key_openkey="",
        key_confidence=0.0,
        energy=0,
        producer="backfill",
        producer_version=backend_version,
        uses_model=False,
        model_sha256=None,
        decode_fingerprint=decode_fingerprint,
        lanes={"beatgrid": LaneResult(status="ok", payload=payload)},
    )


def _key_record(
    stable_id: str,
    *,
    depends_on_beatgrid: dict[str, object],
) -> AnalysisRecord:
    estimate = profiles.KeyEstimate(key=C_MAJOR, confidence=0.9, margin=0.4)
    return record_from_estimate(
        stable_id=stable_id,
        estimate=estimate,
        flag=flags.evaluate_tonal_center(estimate),
        duration_s=16.0,
        sample_rate=44100,
        decode_fingerprint=FINGERPRINT_V1.removeprefix("sha256:"),
        segments_block=_OK_SEGMENTS,
        depends_on_beatgrid=depends_on_beatgrid,
    )


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    open_conn(db_path).close()
    selection.reset_toggles()
    return db_path


def _depends_on_for(record: AnalysisRecord) -> dict[str, object]:
    lane = record.lanes["beatgrid"]
    return depends_on_identity(
        backend=record.backend,
        producer_version=record.producer_version,
        model_sha256=record.model_sha256,
        decode_fingerprint=record.decode_fingerprint,
        beatgrid_payload=lane.payload,
    )


def test_a_newer_beatgrid_version_makes_the_key_lane_stale_on_anlz(state_db: Path) -> None:
    stable_id = "t-stale-version"
    grid_v1 = _beatgrid_record(stable_id, backend_version="1.0.0", decode_fingerprint=FINGERPRINT_V1)
    upsert_record(grid_v1, db_path=state_db)
    key = _key_record(stable_id, depends_on_beatgrid=_depends_on_for(grid_v1))
    upsert_record(key, db_path=state_db)

    grid_v2 = _beatgrid_record(stable_id, backend_version="1.1.0", decode_fingerprint=FINGERPRINT_V1)
    upsert_record(grid_v2, db_path=state_db)

    selection.set_toggle("key", "own")
    payload = overlay_mod.apply_own_key_segments(
        {}, stable_id, state_db_path=state_db
    )
    assert payload["key_segments"]["status"] == "missing"
    assert payload["key_segments"]["reason"] == REASON_STALE_DEPENDENCY


def test_the_same_version_with_a_different_fingerprint_is_stale_too(state_db: Path) -> None:
    stable_id = "t-stale-fingerprint"
    grid_v1 = _beatgrid_record(stable_id, backend_version="1.1.0", decode_fingerprint=FINGERPRINT_V1)
    upsert_record(grid_v1, db_path=state_db)
    key = _key_record(stable_id, depends_on_beatgrid=_depends_on_for(grid_v1))
    upsert_record(key, db_path=state_db)

    grid_v2 = _beatgrid_record(stable_id, backend_version="1.1.0", decode_fingerprint=FINGERPRINT_V2)
    upsert_record(grid_v2, db_path=state_db)

    selection.set_toggle("key", "own")
    payload = overlay_mod.apply_own_key_segments(
        {}, stable_id, state_db_path=state_db
    )
    assert payload["key_segments"]["status"] == "missing"
    assert payload["key_segments"]["reason"] == REASON_STALE_DEPENDENCY


def test_a_beatgrid_upsert_refreshes_the_key_projection(state_db: Path) -> None:
    stable_id = "t-projection-refresh"
    grid_v1 = _beatgrid_record(stable_id, backend_version="1.0.0", decode_fingerprint=FINGERPRINT_V1)
    upsert_record(grid_v1, db_path=state_db)
    key = _key_record(stable_id, depends_on_beatgrid=_depends_on_for(grid_v1))
    upsert_record(key, db_path=state_db)

    selection.set_toggle("key", "own")
    conn = open_conn(state_db)
    try:
        fields_before = selection.effective_fields(
            conn, [stable_id], selection.Selection.resolve(conn)
        )
        assert fields_before[stable_id]["key"].value == "8B"
    finally:
        conn.close()

    grid_v2 = _beatgrid_record(stable_id, backend_version="1.1.0", decode_fingerprint=FINGERPRINT_V1)
    upsert_record(grid_v2, db_path=state_db)

    conn = open_conn(state_db)
    try:
        fields_after = selection.effective_fields(
            conn, [stable_id], selection.Selection.resolve(conn)
        )
        assert fields_after[stable_id]["key"].status == "missing"
        assert canonical_pointer(conn, stable_id, "key") is None
    finally:
        conn.close()


def test_canonical_rebuild_excludes_a_stale_key_row(state_db: Path) -> None:
    stable_id = "t-stale-canonical"
    grid_v1 = _beatgrid_record(stable_id, backend_version="1.0.0", decode_fingerprint=FINGERPRINT_V1)
    upsert_record(grid_v1, db_path=state_db)
    key = _key_record(stable_id, depends_on_beatgrid=_depends_on_for(grid_v1))
    upsert_record(key, db_path=state_db)

    grid_v2 = _beatgrid_record(stable_id, backend_version="1.1.0", decode_fingerprint=FINGERPRINT_V1)
    upsert_record(grid_v2, db_path=state_db)

    conn = open_conn(state_db)
    try:
        recompute_canonical(conn, stable_id, "key")
        assert canonical_pointer(conn, stable_id, "key") is None
    finally:
        conn.close()
