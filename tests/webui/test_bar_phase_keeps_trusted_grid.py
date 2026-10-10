"""A trusted beatgrid is not replaced by a later untrusted bar phase (NATIVE-34).

[if] a later beatgrid is bar_phase_untrusted [then] the stored trusted grid stays, [else stop].
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import fetch_records_by_ids, upsert_record

pytestmark = pytest.mark.requirement("NATIVE-34")


def _rec(sid: str, *, bpm: float) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=sid,
        backend="librosa+madmom",
        backend_version="test-1.0",
        analyzed_at=datetime(2026, 4, 17, tzinfo=UTC),
        duration_s=60.0,
        sample_rate=44100,
        bpm=bpm,
        bpm_confidence=0.9,
        key_camelot="8A",
        key_openkey="8m",
        key_confidence=0.9,
        energy=6,
    )

def test_an_untrusted_bar_phase_does_not_replace_a_trusted_grid(tmp_path: Path) -> None:
    from apps.analysis.lanes import LaneResult

    db = tmp_path / "state.db"

    def grid(untrusted: bool, bpm: float) -> AnalysisRecord:
        payload = {
            "beats": [{"t": 0.0, "n": 1, "bpm": bpm}, {"t": 0.5, "n": 2, "bpm": bpm}],
            "bpm": bpm,
            "static_grid_untrusted": False,
        }
        if untrusted:
            payload["bar_phase_untrusted"] = True
        return dataclasses.replace(
            _rec("sid-keep", bpm=bpm),
            lanes={"beatgrid": LaneResult(status="ok", reason=None, confidence=0.9, payload=payload)},
        )

    upsert_record(grid(False, 120.0), db_path=db)
    result = upsert_record(grid(True, 99.0), db_path=db)
    assert result.unchanged
    stored = fetch_records_by_ids(["sid-keep"], db_path=db)[0]
    assert stored.bpm == 120.0
    assert stored.lanes["beatgrid"].payload.get("bar_phase_untrusted") is not True


def test_corrupt_stored_json_is_not_treated_as_an_absent_grid(tmp_path: Path) -> None:
    """[if] stored analysis JSON is corrupt [then] upsert raises and the row stays, [else stop]."""
    import json

    from apps.analysis.store import open_conn

    db = tmp_path / "state.db"

    def grid(bpm: float) -> AnalysisRecord:
        payload = {
            "beats": [{"t": 0.0, "n": 1, "bpm": bpm}, {"t": 0.5, "n": 2, "bpm": bpm}],
            "bpm": bpm,
            "static_grid_untrusted": False,
        }
        return dataclasses.replace(
            _rec("sid-keep", bpm=bpm),
            lanes={"beatgrid": LaneResult(status="ok", reason=None, confidence=0.9, payload=payload)},
        )

    upsert_record(grid(120.0), db_path=db)
    conn = open_conn(db)
    try:
        conn.execute(
            "UPDATE analysis SET record_json = ? WHERE stable_id = ?",
            ("{", "sid-keep"),
        )
        conn.commit()
    finally:
        conn.close()
    from apps.analysis.store import StoredRecordError

    with pytest.raises(StoredRecordError) as caught:
        upsert_record(grid(99.0), db_path=db)
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)
    conn = open_conn(db)
    try:
        raw = conn.execute(
            "SELECT record_json, bpm FROM analysis WHERE stable_id = ?",
            ("sid-keep",),
        ).fetchone()
    finally:
        conn.close()
    assert raw == ("{", 120.0)


def _ok_grid(sid: str, bpm: float, **extra: object) -> AnalysisRecord:
    payload: dict[str, object] = {
        "beats": [{"t": 0.0, "n": 1, "bpm": bpm}, {"t": 0.5, "n": 2, "bpm": bpm}],
        "bpm": bpm,
        "static_grid_untrusted": False,
        **extra,
    }
    return dataclasses.replace(
        _rec(sid, bpm=bpm),
        lanes={"beatgrid": LaneResult(status="ok", reason=None, confidence=0.9, payload=payload)},
    )


def _stored_json(db: Path, sid: str) -> str:
    from apps.analysis.store import open_conn

    conn = open_conn(db)
    try:
        return str(
            conn.execute("SELECT record_json FROM analysis WHERE stable_id = ?", (sid,)).fetchone()[0]
        )
    finally:
        conn.close()


def _overwrite_stored_json(db: Path, sid: str, raw: str) -> None:
    from apps.analysis.store import open_conn

    conn = open_conn(db)
    try:
        conn.execute("UPDATE analysis SET record_json = ? WHERE stable_id = ?", (raw, sid))
        conn.commit()
    finally:
        conn.close()


def _with_lanes(raw: str, lanes: object) -> str:
    import json

    data = json.loads(raw)
    data["lanes"] = lanes
    return json.dumps(data)


def _with_beatgrid(raw: str, **changes: object) -> str:
    import json

    data = json.loads(raw)
    data["lanes"]["beatgrid"].update(changes)
    return json.dumps(data)


_MALFORMED = {
    "lanes is a list": lambda raw: _with_lanes(raw, []),
    "beatgrid lane is a string": lambda raw: _with_lanes(raw, {"beatgrid": "ok"}),
    "status is a number": lambda raw: _with_beatgrid(raw, status=1),
    "status is unknown": lambda raw: _with_beatgrid(raw, status="okay"),
    "payload is a list": lambda raw: _with_beatgrid(raw, payload=[]),
    "beats is a string": lambda raw: _with_beatgrid(raw, payload={"beats": "none"}),
    "trust flag is a string": lambda raw: _with_beatgrid(
        raw, payload={"beats": [{"t": 0.0}], "bar_phase_untrusted": "false"}
    ),
}


@pytest.mark.parametrize("case", sorted(_MALFORMED))
def test_a_malformed_stored_beatgrid_raises_and_is_not_overwritten(
    tmp_path: Path, case: str
) -> None:
    """[if] a stored record has a wrong shape [then] upsert raises and the row stays, [else stop]."""
    from apps.analysis.lane_enums import LaneContractError
    from apps.analysis.record import RecordContractError

    db = tmp_path / "state.db"
    upsert_record(_ok_grid("sid-keep", 120.0), db_path=db)
    corrupt = _MALFORMED[case](_stored_json(db, "sid-keep"))
    _overwrite_stored_json(db, "sid-keep", corrupt)

    # The queue runner fails one track on exactly these two types.
    with pytest.raises((RecordContractError, LaneContractError)):
        upsert_record(_ok_grid("sid-keep", 99.0, bar_phase_untrusted=True), db_path=db)
    assert _stored_json(db, "sid-keep") == corrupt, "the corrupt row must not be overwritten"


def test_an_absent_beatgrid_lane_is_still_replaced(tmp_path: Path) -> None:
    """[if] the stored record has no beatgrid lane [then] the new grid is written, [else stop]."""
    import json

    db = tmp_path / "state.db"
    for lanes in ({}, None, {"beatgrid": {"status": "failed", "reason": "too_few_beats"}}):
        upsert_record(_ok_grid("sid-keep", 120.0), db_path=db)
        _overwrite_stored_json(db, "sid-keep", _with_lanes(_stored_json(db, "sid-keep"), lanes))
        result = upsert_record(_ok_grid("sid-keep", 99.0, bar_phase_untrusted=True), db_path=db)
        assert not result.unchanged, f"absent lane {lanes!r} must not block the write"
        stored = json.loads(_stored_json(db, "sid-keep"))
        assert stored["lanes"]["beatgrid"]["payload"]["bpm"] == 99.0


def test_a_non_bool_trust_flag_is_not_served_as_trusted() -> None:
    """[if] the stored trust flag is not a bool [then] the /anlz block raises, [else stop]."""
    from apps.analysis.lane_enums import LaneContractError
    from apps.webui.server.rb_vendor_pkg.own_beatgrid_overlay import _own_beatgrid_block

    def lane(**extra: object) -> LaneResult:
        payload = {
            "beats": [{"t": 0.0, "n": 1, "bpm": 120.0}, {"t": 0.5, "n": 2, "bpm": 120.0}],
            "bpm": 120.0,
            "tempo_changes": [],
            "static_grid_untrusted": False,
            **extra,
        }
        return LaneResult(status="ok", reason=None, confidence=0.9, payload=payload)

    with pytest.raises(LaneContractError, match="bar_phase_untrusted"):
        _own_beatgrid_block(lane(bar_phase_untrusted="true"), "sid")
    block, _changes = _own_beatgrid_block(lane(bar_phase_untrusted=True), "sid")
    assert block["bar_phase_untrusted"] is True
    block, _changes = _own_beatgrid_block(lane(), "sid")
    assert "bar_phase_untrusted" not in block
