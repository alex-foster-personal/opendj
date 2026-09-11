"""`/anlz` serves the own key-change segments when the key lane's source is own.

NATIVE-05's wire half. Key-change segments are timeline data, so they ride the
`/anlz` payload, unlike the scalar key which is read-time projected through
`analysis_projection`/`effective_fields` (`tests/analysis/
test_key_projection_no_track_fields_write.py`).

Acceptance lines:
  [if] a track has two own key segments [then] its own `/anlz` payload returns
       them in `key_segments` ⛔️ a bare absent array
  [if] an own payload's key lane is `ok` [then] `key_segments` is present and
       carries its own `status` ⛔️ a block a consumer has to infer
  [if] the payload is rekordbox-sourced [then] `key_segments` is ABSENT ⛔️ a
       field that changes rekordbox behavior
  [if] own is selected and no own key record exists [then] `missing` with a
       reason, never rekordbox's key standing in

Every own record here is built by the real producer function
(`apps.analysis.backends.own_key.record_from_estimate`) and written through the
real store, so what these tests read back is what a backfill run would have put
there.

-Claude
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.analysis import selection
from apps.analysis.backends.own_key import record_from_estimate
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import open_conn, upsert_record
from apps.analysis_key import canon, flags, profiles, segments
from apps.analysis_key.lane_payload import depends_on_identity
from apps.webui.server.rb_vendor_pkg import own_key_overlay as overlay_mod
from apps.webui.server.rb_vendor_pkg import own_overlays

C_MAJOR = canon.Key(0, False)
A_MINOR = canon.Key(9, True)
FINGERPRINT_HEX = "2f" * 32
FINGERPRINT = "sha256:" + FINGERPRINT_HEX


@pytest.fixture(autouse=True)
def _launch_state_toggles() -> Any:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    open_conn(db_path).close()
    return db_path


def _two_segment_block() -> dict[str, Any]:
    """A real `ok` segments block: C major for 16 bars, A minor for 16."""
    grid = segments.BarGrid(
        starts=tuple(float(bar) * 2.0 for bar in range(32)),
        ends=tuple(float(bar) * 2.0 for bar in range(1, 33)),
    )
    block = segments.segment_bars(
        _bar_chroma([C_MAJOR] * 16 + [A_MINOR] * 16), grid, duration_s=64.0
    )
    assert block.status == "ok" and len(block.segments) == 2, block
    return block.to_payload()


def _bar_chroma(keys: list[canon.Key]) -> Any:
    import numpy as np

    columns = []
    for key in keys:
        third = 3 if key.is_minor else 4
        vector = np.zeros(12)
        for interval in (0, third, 7):
            vector[(key.pitch_class + interval) % 12] = 1.0
        columns.append(vector / np.linalg.norm(vector))
    return np.stack(columns, axis=1)


def _matching_beatgrid_record(stable_id: str) -> AnalysisRecord:
    from datetime import UTC, datetime

    beats = [
        {
            "t": round(index * 0.5, 5),
            "n": (index % 4) + 1,
            "bpm": 120.0,
        }
        for index in range(32)
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
        backend_version="1.0.0",
        analyzed_at=datetime.now(UTC),
        duration_s=64.0,
        sample_rate=44100,
        bpm=120.0,
        bpm_confidence=0.9,
        key_camelot="",
        key_openkey="",
        key_confidence=0.0,
        energy=0,
        producer="backfill",
        producer_version="1.0.0",
        uses_model=False,
        model_sha256=None,
        decode_fingerprint=FINGERPRINT,
        lanes={"beatgrid": LaneResult(status="ok", payload=payload)},
    )


def _own_key_record(
    stable_id: str, *, block: dict[str, Any], key: canon.Key = C_MAJOR
) -> Any:
    estimate = profiles.KeyEstimate(key=key, confidence=0.9, margin=0.4)
    depends_on_beatgrid = None
    if block.get("status") == "ok":
        grid = _matching_beatgrid_record(stable_id)
        depends_on_beatgrid = depends_on_identity(
            backend=grid.backend,
            producer_version=grid.producer_version,
            model_sha256=grid.model_sha256,
            decode_fingerprint=grid.decode_fingerprint,
            beatgrid_payload=grid.lanes["beatgrid"].payload,
        )
    return record_from_estimate(
        stable_id=stable_id,
        estimate=estimate,
        flag=flags.evaluate_tonal_center(estimate),
        duration_s=64.0,
        sample_rate=44100,
        decode_fingerprint=FINGERPRINT_HEX,
        segments_block=block,
        depends_on_beatgrid=depends_on_beatgrid,
    )


def _seed_own_key_with_beatgrid(
    state_db: Path, stable_id: str, *, block: dict[str, Any]
) -> None:
    upsert_record(_matching_beatgrid_record(stable_id), db_path=state_db)
    upsert_record(_own_key_record(stable_id, block=block), db_path=state_db)


def _rekordbox_payload(stable_id: str) -> dict[str, Any]:
    return {
        "stable_id": stable_id,
        "beatgrid": {"source": "rekordbox", "beat_count": 0, "beats": []},
        "key_segments": {"status": "ok", "reason": None, "segments": [{"stale": True}]},
    }


#-----------------------------------------------------------------------------
# own
#-----------------------------------------------------------------------------

def test_two_own_segments_are_served_on_the_own_payload(state_db: Path) -> None:
    stable_id = "t-two"
    _seed_own_key_with_beatgrid(state_db, stable_id, block=_two_segment_block())
    selection.set_toggle("key", "own")

    payload = overlay_mod.apply_own_key_segments(
        _rekordbox_payload(stable_id), stable_id, state_db_path=state_db
    )

    block = payload["key_segments"]
    assert block["status"] == "ok"
    assert block["reason"] is None
    assert len(block["segments"]) == 2
    assert block["segments"][0]["key_camelot"] == canon.to_camelot(C_MAJOR)
    assert block["segments"][1]["key_camelot"] == canon.to_camelot(A_MINOR)
    assert block["segments"][0]["end_bar"] == block["segments"][1]["start_bar"]


def test_an_ok_lane_always_carries_a_segments_block_with_its_own_status(
    state_db: Path,
) -> None:
    """`missing` is a state the block reports, not a reason to omit the block."""
    stable_id = "t-missing"
    upsert_record(
        _own_key_record(
            stable_id,
            block=segments.missing_block(segments.REASON_NO_DOWNBEATS).to_payload(),
        ),
        db_path=state_db,
    )
    selection.set_toggle("key", "own")

    payload = overlay_mod.apply_own_key_segments(
        _rekordbox_payload(stable_id), stable_id, state_db_path=state_db
    )

    assert payload["key_segments"]["status"] == "missing"
    assert payload["key_segments"]["reason"] == segments.REASON_NO_DOWNBEATS
    assert payload["key_segments"]["segments"] == []


def test_a_failed_segments_block_keeps_its_reason(state_db: Path) -> None:
    stable_id = "t-failed-segments"
    upsert_record(
        _own_key_record(
            stable_id,
            block=segments.segment_bars(
                _bar_chroma([C_MAJOR] * 4),
                segments.BarGrid(
                    starts=(0.0, 2.0, 4.0, 6.0), ends=(2.0, 4.0, 6.0, 8.0)
                ),
                duration_s=8.0,
            ).to_payload(),
        ),
        db_path=state_db,
    )
    selection.set_toggle("key", "own")

    payload = overlay_mod.apply_own_key_segments(
        _rekordbox_payload(stable_id), stable_id, state_db_path=state_db
    )

    assert payload["key_segments"]["status"] == "failed"
    assert payload["key_segments"]["reason"] == segments.REASON_TOO_FEW_BARS


def test_own_selected_with_no_record_is_missing_with_a_reason(state_db: Path) -> None:
    selection.set_toggle("key", "own")
    payload = overlay_mod.apply_own_key_segments(
        _rekordbox_payload("t-none"), "t-none", state_db_path=state_db
    )
    assert payload["key_segments"]["status"] == "missing"
    assert payload["key_segments"]["reason"] == overlay_mod.OWN_KEY_MISSING_REASON
    assert payload["key_segments"]["segments"] == []


def test_a_failed_key_lane_is_not_served_as_a_missing_one(state_db: Path) -> None:
    """`failed` (the analyzer ran and declined) and `missing` (it has not run)
    are different states all the way to the wire."""
    stable_id = "t-failed-lane"
    estimate = profiles.KeyEstimate(key=C_MAJOR, confidence=0.0, margin=0.0)
    upsert_record(
        record_from_estimate(
            stable_id=stable_id,
            estimate=estimate,
            flag=flags.evaluate_tonal_center(estimate),
            duration_s=64.0,
            sample_rate=44100,
            decode_fingerprint=FINGERPRINT_HEX,
            segments_block=segments.missing_block(segments.REASON_NO_DOWNBEATS).to_payload(),
        ),
        db_path=state_db,
    )
    selection.set_toggle("key", "own")

    payload = overlay_mod.apply_own_key_segments(
        _rekordbox_payload(stable_id), stable_id, state_db_path=state_db
    )

    assert payload["key_segments"]["status"] == "failed"
    assert payload["key_segments"]["reason"].startswith(overlay_mod.NO_TONAL_CENTER)


def test_an_ok_lane_with_no_segments_is_refused_not_served(state_db: Path) -> None:
    """The contract violation, caught on the READ side too: a healthy-looking
    empty array would silently disable the deck's key-change markers.

    The store's own validator refuses to WRITE this shape (asserted in
    `tests/analysis_key/test_backfill_write.py`), so the row is corrupted the
    only way it can exist -- by rewriting the stored JSON of a good record --
    and the claim under test is that the read path does not serve it either.
    A write-side refusal is not evidence about a row written by an older
    version or edited out of band.
    """
    stable_id = "t-empty-ok"
    _seed_own_key_with_beatgrid(state_db, stable_id, block=_two_segment_block())
    conn = open_conn(state_db)
    try:
        stored = json.loads(
            conn.execute(
                "SELECT record_json FROM analysis WHERE stable_id = ? AND backend LIKE 'own_key.%'",
                (stable_id,),
            ).fetchone()[0]
        )
        stored["lanes"]["key"]["payload"]["segments"] = {
            "status": "ok",
            "reason": None,
            "segments": [],
        }
        conn.execute(
            "UPDATE analysis SET record_json = ? WHERE stable_id = ? AND backend LIKE 'own_key.%'",
            (json.dumps(stored, sort_keys=True, separators=(",", ":")), stable_id),
        )
        conn.commit()
    finally:
        conn.close()
    selection.set_toggle("key", "own")

    with pytest.raises(RuntimeError, match="segments"):
        overlay_mod.apply_own_key_segments(
        _rekordbox_payload(stable_id), stable_id, state_db_path=state_db
    )


#-----------------------------------------------------------------------------
# rekordbox
#-----------------------------------------------------------------------------

def test_a_rekordbox_payload_never_carries_the_field(state_db: Path) -> None:
    stable_id = "t-rbx"
    _seed_own_key_with_beatgrid(state_db, stable_id, block=_two_segment_block())
    # The record exists, but the lane's effective source is still rekordbox
    # (D1: no promotion), so the field must be ABSENT rather than served.
    payload = overlay_mod.apply_own_key_segments(
        _rekordbox_payload(stable_id), stable_id, state_db_path=state_db
    )
    assert "key_segments" not in payload


def test_a_stale_cached_block_is_removed_when_the_source_is_rekordbox(
    state_db: Path,
) -> None:
    """The overlay is applied AFTER the payload cache, which is keyed on the
    ANLZ mtime and points -- neither of which moves when the toggle flips. A
    cached entry written while own was selected must not survive the flip."""
    stable_id = "t-stale"
    _seed_own_key_with_beatgrid(state_db, stable_id, block=_two_segment_block())
    payload = overlay_mod.apply_own_key_segments(
        _rekordbox_payload(stable_id), stable_id, state_db_path=state_db
    )
    assert "key_segments" not in payload


#-----------------------------------------------------------------------------
# the composition the /anlz call site uses
#-----------------------------------------------------------------------------

def test_the_composed_overlay_still_replaces_the_beatgrid_block(state_db: Path) -> None:
    """`anlz.py` calls ONE function for both own lanes; rewiring it must not
    drop the beatgrid overlay's work."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from apps.analysis.lanes import LaneResult
    from apps.analysis.record import AnalysisRecord

    stable_id = "t-both"
    beats = [
        {"t": round(index * 0.5, 5), "n": (index % 4) + 1, "bpm": 120.0}
        for index in range(8)
    ]
    upsert_record(
        AnalysisRecord(
            stable_id=stable_id, backend="own_beatgrid.backfill",
            backend_version="1.1.0", analyzed_at=_datetime.now(_UTC),
            duration_s=4.0, sample_rate=44100, bpm=120.0, bpm_confidence=0.9,
            key_camelot="", key_openkey="", key_confidence=0.0, energy=0,
            producer="backfill", producer_version="1.1.0", uses_model=False,
            model_sha256=None, decode_fingerprint=FINGERPRINT,
            lanes={"beatgrid": LaneResult(status="ok", payload={
                "beats": beats, "bpm": 120.0, "bpm_confidence": 0.9,
                "octave_reason": "in_band", "first_downbeat_s": 0.0,
                "tempo_changes": [], "static_grid_untrusted": False,
            })},
        ),
        db_path=state_db,
    )
    selection.set_toggle("beatgrid", "own")
    selection.set_toggle("key", "own")

    payload = own_overlays.apply_own_overlays(
        _rekordbox_payload(stable_id), stable_id, state_db_path=state_db
    )

    assert payload["beatgrid"]["source"] == "own"
    assert payload["beatgrid"]["status"] == "ok"
    assert payload["beatgrid"]["beat_count"] == 8
    assert payload["key_segments"]["status"] == "missing"
