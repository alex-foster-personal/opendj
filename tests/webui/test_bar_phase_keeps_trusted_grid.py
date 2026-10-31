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


@pytest.mark.parametrize("untrusted", [True, False])
def test_a_refused_grid_keeps_the_rest_of_its_record(tmp_path: Path, untrusted: bool) -> None:
    """[if] an untrusted grid rides with a new key [then] the key lands and the grid stays, [else stop]."""
    db = tmp_path / "state.db"
    upsert_record(_ok_grid("sid-keep", 120.0), db_path=db)
    extra = {"bar_phase_untrusted": True} if untrusted else {}
    incoming = dataclasses.replace(
        _ok_grid("sid-keep", 99.0, **extra), key_camelot="5A", key_openkey="5m", energy=8
    )
    assert not upsert_record(incoming, db_path=db).unchanged
    stored = fetch_records_by_ids(["sid-keep"], db_path=db)[0]
    assert (stored.key_camelot, stored.key_openkey, stored.energy) == ("5A", "5m", 8)
    grid = stored.lanes["beatgrid"].payload
    if untrusted:
        assert stored.bpm == 120.0 and grid["bpm"] == 120.0 and "bar_phase_untrusted" not in grid
    else:
        assert stored.bpm == 99.0 and grid["bpm"] == 99.0, "a trusted grid still replaces"


def _own_grid(version: str, bpm: float, **extra: object) -> AnalysisRecord:
    """A real own beatgrid record (the producer path) restamped at ``version`` with a chosen grid.

    Own backends are the only ones the canonical pointer serves, so a
    pre-v1 backend cannot exercise the version-bump path.
    """
    from apps.analysis.backends.own_beatgrid import record_from_payload
    from tests.analysis_beatgrid.round1_artifact import load_round1_raw

    whole = load_round1_raw()
    first = next(iter(whole["results"]))
    base = record_from_payload(
        {**whole, "results": {first: whole["results"][first]}},
        stable_id="sid-bump",
        audio_path=Path("/fixtures/bump.wav"),
        model_sha256="8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331",
        decode_fingerprint="1f" * 32,
    )
    lane = base.lanes["beatgrid"]
    payload = {**lane.payload, "bpm": bpm, "beats": [{"t": 0.0, "n": 1, "bpm": bpm}, {"t": 0.5, "n": 2, "bpm": bpm}]}
    payload.pop("bar_phase_untrusted", None)
    payload.update(extra)
    return dataclasses.replace(
        base,
        backend_version=version,
        producer_version=version,
        bpm=bpm,
        lanes={**base.lanes, "beatgrid": dataclasses.replace(lane, status="ok", payload=payload)},
    )


@pytest.mark.parametrize("untrusted", [True, False])
def test_a_producer_version_bump_keeps_the_trusted_grid_it_would_outrank(
    tmp_path: Path, untrusted: bool
) -> None:
    """[if] a 1.7.0 untrusted grid lands over a trusted 1.6.0 one [then] the served grid stays trusted, [else stop].

    The bump writes a new (backend, backend_version) row that wins the
    canonical pointer by semver, so the same-version row lookup alone sees
    nothing to protect. The trusted-grid control proves a 1.7.0 grid still
    replaces when it is trusted.
    """
    from apps.analysis.canonical import canonical_pointer
    from apps.analysis.store import open_conn

    db = tmp_path / "state.db"
    upsert_record(_own_grid("1.6.0", 120.0), db_path=db)
    extra = {"bar_phase_untrusted": True} if untrusted else {}
    upsert_record(_own_grid("1.7.0", 99.0, **extra), db_path=db)
    conn = open_conn(db)
    try:
        pointer = canonical_pointer(conn, "sid-bump", "beatgrid")
        assert pointer is not None, "an own beatgrid record is served"
        (served,) = conn.execute(
            "SELECT record_json FROM analysis WHERE stable_id = ? AND backend = ? AND backend_version = ?",
            ("sid-bump", *pointer),
        ).fetchone()
    finally:
        conn.close()
    assert pointer[1] == "1.7.0", "the new producer version still wins the pointer"
    record = AnalysisRecord.from_json(served)
    grid = record.lanes["beatgrid"].payload
    if untrusted:
        assert record.bpm == 120.0 and grid["bpm"] == 120.0 and "bar_phase_untrusted" not in grid
    else:
        assert record.bpm == 99.0 and grid["bpm"] == 99.0, "a trusted 1.7.0 grid still replaces"


@pytest.mark.parametrize("untrusted", [True, False])
def test_a_same_version_row_without_a_trusted_grid_still_defers_to_the_served_one(
    tmp_path: Path, untrusted: bool
) -> None:
    """[if] the 1.7.0 row exists with a failed grid while 1.6.0 is served [then] an untrusted 1.7.0 grid keeps 1.6.0's, [else stop].

    The queue runner clears the stale marker and then upserts, so the
    same-version row exists but holds no trusted grid; the served 1.6.0 one
    is what this write would displace. The trusted control proves a trusted
    1.7.0 grid still replaces.
    """
    from apps.analysis.canonical import canonical_pointer, refresh_lanes
    from apps.analysis.store import open_conn
    from apps.shared.state import analysis_stale

    db = tmp_path / "state.db"
    upsert_record(_own_grid("1.6.0", 120.0), db_path=db)
    failed = _own_grid("1.7.0", 99.0)
    failed = dataclasses.replace(
        failed,
        lanes={**failed.lanes, "beatgrid": dataclasses.replace(failed.lanes["beatgrid"], status="failed", reason="x", payload={})},
    )
    upsert_record(failed, db_path=db)
    conn = open_conn(db)
    try:
        backend = failed.backend
        analysis_stale.mark_stale(
            conn, stable_id="sid-bump", lane="beatgrid", backend=backend,
            backend_version="1.7.0", reason=analysis_stale.STALE_DEPENDENCY_MOVED,
        )
        refresh_lanes(conn, "sid-bump", ["beatgrid"])
        assert canonical_pointer(conn, "sid-bump", "beatgrid") == (backend, "1.6.0")
        analysis_stale.clear_stale(
            conn, stable_id="sid-bump", lane="beatgrid", backend=backend, backend_version="1.7.0"
        )
        conn.commit()
    finally:
        conn.close()
    extra = {"bar_phase_untrusted": True} if untrusted else {}
    upsert_record(_own_grid("1.7.0", 99.0, **extra), db_path=db)
    conn = open_conn(db)
    try:
        pointer = canonical_pointer(conn, "sid-bump", "beatgrid")
        assert pointer == (backend, "1.7.0")
        (served,) = conn.execute(
            "SELECT record_json FROM analysis WHERE stable_id = ? AND backend = ? AND backend_version = ?",
            ("sid-bump", *pointer),
        ).fetchone()
    finally:
        conn.close()
    record = AnalysisRecord.from_json(served)
    grid = record.lanes["beatgrid"].payload
    if untrusted:
        assert record.bpm == 120.0 and grid["bpm"] == 120.0 and "bar_phase_untrusted" not in grid
    else:
        assert record.bpm == 99.0 and grid["bpm"] == 99.0, "a trusted 1.7.0 grid still replaces"


def _retire(db: Path, stable_id: str) -> None:
    """Folder ingest's over-budget retirement of a rewritten file's records (NATIVE-30)."""
    from apps.analysis.store import open_conn
    from apps.shared.state.ingest import folder

    conn = open_conn(db)
    try:
        folder._retire_analysis_of_replaced_bytes(conn, stable_id, delete=False)
        conn.commit()
    finally:
        conn.close()


def _served_grid(db: Path, stable_id: str) -> tuple[tuple[str, str] | None, AnalysisRecord | None]:
    from apps.analysis.canonical import canonical_pointer
    from apps.analysis.store import open_conn

    conn = open_conn(db)
    try:
        pointer = canonical_pointer(conn, stable_id, "beatgrid")
        if pointer is None:
            return None, None
        (served,) = conn.execute(
            "SELECT record_json FROM analysis WHERE stable_id = ? AND backend = ? AND backend_version = ?",
            (stable_id, *pointer),
        ).fetchone()
    finally:
        conn.close()
    return pointer, AnalysisRecord.from_json(served)


@pytest.mark.parametrize("retired", [True, False])
def test_a_retired_records_grid_is_not_kept_over_a_reanalysis_of_the_new_bytes(
    tmp_path: Path, retired: bool
) -> None:
    """[if] the trusted grid's row was retired by an audio rewrite [then] an untrusted re-analysis lands as is, [else stop].

    The retired grid describes the previous bytes. The control: an unretired
    trusted grid is still kept over the same untrusted result.
    """
    db = tmp_path / "state.db"
    upsert_record(_own_grid("1.7.0", 120.0), db_path=db)
    if retired:
        _retire(db, "sid-bump")
    upsert_record(_own_grid("1.7.0", 99.0, bar_phase_untrusted=True), db_path=db)
    pointer, record = _served_grid(db, "sid-bump")
    assert pointer is not None and record is not None
    grid = record.lanes["beatgrid"].payload
    if retired:
        assert record.bpm == 99.0 and grid["bpm"] == 99.0 and grid["bar_phase_untrusted"] is True
    else:
        assert record.bpm == 120.0 and grid["bpm"] == 120.0, "an unretired trusted grid is still kept"


def test_a_producer_rewrite_clears_the_audio_replaced_markers_of_the_lanes_it_supplies(tmp_path: Path) -> None:
    """[if] a retired record is written again [then] its supplied lanes are eligible again and others stay stale, [else stop]."""
    from apps.analysis.store import open_conn
    from apps.shared.state import analysis_stale

    db = tmp_path / "state.db"
    fresh = _own_grid("1.7.0", 120.0)
    assert "key" not in fresh.lanes, "the control lane must be one the record does not supply"
    upsert_record(fresh, db_path=db)
    _retire(db, "sid-bump")
    conn = open_conn(db)
    try:
        # A lane the previous bytes' record carried and this one does not.
        analysis_stale.mark_stale(
            conn, stable_id="sid-bump", lane="key", backend=fresh.backend,
            backend_version="1.7.0", reason=analysis_stale.STALE_AUDIO_REPLACED,
        )
        conn.commit()
    finally:
        conn.close()
    assert _served_grid(db, "sid-bump")[0] is None, "retired: nothing is served"

    upsert_record(dataclasses.replace(fresh, bpm=121.0), db_path=db)

    assert _served_grid(db, "sid-bump")[0] == (fresh.backend, "1.7.0"), "the rewritten lane is eligible again"
    conn = open_conn(db)
    try:
        left = sorted(row[0] for row in conn.execute(
            "SELECT lane FROM analysis_stale WHERE stable_id = ?", ("sid-bump",)
        ))
    finally:
        conn.close()
    assert left == ["key"], "a lane the fresh record does not supply stays stale"


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


def _without_lanes(raw: str) -> str:
    import json

    data = json.loads(raw)
    del data["lanes"]
    return json.dumps(data)


def _with_beatgrid(raw: str, **changes: object) -> str:
    import json

    data = json.loads(raw)
    data["lanes"]["beatgrid"].update(changes)
    return json.dumps(data)


_MALFORMED = {
    "lanes is a list": lambda raw: _with_lanes(raw, []),
    "lanes is null": lambda raw: _with_lanes(raw, None),
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
    absent = {
        "no lanes key": _without_lanes,
        "empty lanes": lambda raw: _with_lanes(raw, {}),
        "failed beatgrid": lambda raw: _with_lanes(raw, {"beatgrid": {"status": "failed", "reason": "too_few_beats"}}),
    }
    for case, strip in absent.items():
        upsert_record(_ok_grid("sid-keep", 120.0), db_path=db)
        _overwrite_stored_json(db, "sid-keep", strip(_stored_json(db, "sid-keep")))
        result = upsert_record(_ok_grid("sid-keep", 99.0, bar_phase_untrusted=True), db_path=db)
        assert not result.unchanged, f"{case} must not block the write"
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


# --- the cascade reads the grid the row holds, not the one it refused --------


def _commit_through_queue(db: Path, record: AnalysisRecord, tmp_path: Path) -> None:
    """Write ``record`` the way the drain does: claim a real queue item, then commit it."""
    from apps.analysis import admission, queue_store
    from apps.analysis import queue as queue_api
    from apps.analysis.queue_runner import _commit_record
    from apps.analysis.store import open_conn

    audio = tmp_path / "grid.wav"
    audio.write_bytes(b"\0")
    conn = open_conn(db)
    try:
        batch = queue_api.enqueue(
            conn,
            [
                admission.Candidate(
                    stable_id=record.stable_id,
                    lane="beatgrid",
                    backend=record.backend,
                    file_path=str(audio),
                    duration_s=180.0,
                )
            ],
        )
        assert batch.admitted == 1, "control: the beatgrid item was admitted"
        item = queue_store.claim_next(conn, batch.batch_id, runner_id="runner-a")
        assert item is not None
        outcomes = _commit_record(conn, batch_id=batch.batch_id, item=item, record=record, runner_id="runner-a")
        assert outcomes is not None, "control: the live claim committed"
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["upsert_record", "queue commit"])
@pytest.mark.parametrize("untrusted", [True, False])
def test_a_refused_grid_does_not_stale_the_key_that_depends_on_the_kept_one(
    tmp_path: Path, untrusted: bool, path: str
) -> None:
    """[if] an untrusted grid is refused [then] the dependent key stays canonical, [else stop].

    The incoming record also changes ``features_blob``, so the write is not
    ``unchanged`` and its post-write effects run. They must hash the grid the
    row kept; hashing the refused one reads a moved dependency and stales a
    key that is still current. The trusted control proves a real grid change
    still stales the key, on both write paths.
    """
    from apps.analysis.canonical import canonical_pointer
    from apps.analysis.store import open_conn
    from tests.analysis.test_key_depends_on_staleness import (
        FINGERPRINT_V1,
        _beatgrid_record,
        _depends_on_for,
        _key_record,
    )

    db = tmp_path / "state.db"
    sid = "sid-cascade"
    grid = _beatgrid_record(sid, backend_version="1.0.0", decode_fingerprint=FINGERPRINT_V1)
    upsert_record(grid, db_path=db)
    upsert_record(_key_record(sid, depends_on_beatgrid=_depends_on_for(grid)), db_path=db)
    conn = open_conn(db)
    try:
        assert canonical_pointer(conn, sid, "key") is not None, "control: the key starts canonical"
    finally:
        conn.close()

    lane = grid.lanes["beatgrid"]
    moved = {**lane.payload, "bpm": 99.0, "beats": [{"t": 0.0, "n": 1, "bpm": 99.0}, {"t": 0.6, "n": 2, "bpm": 99.0}]}
    if untrusted:
        moved["bar_phase_untrusted"] = True
    incoming = dataclasses.replace(
        grid,
        bpm=99.0,
        features_blob={**grid.features_blob, "retained_activations": [0.1, 0.2]},
        lanes={**grid.lanes, "beatgrid": dataclasses.replace(lane, payload=moved)},
    )
    if path == "upsert_record":
        result = upsert_record(incoming, db_path=db)
        assert not result.unchanged
        # The record the effects read is the row as written, in both
        # directions: the kept grid when refused, the new grid when trusted,
        # and the rest of the incoming record either way.
        conn = open_conn(db)
        try:
            (row_json,) = conn.execute(
                "SELECT record_json FROM analysis WHERE stable_id = ? AND backend = ? AND backend_version = ?",
                (sid, grid.backend, grid.backend_version),
            ).fetchone()
        finally:
            conn.close()
        assert result.record.lanes["beatgrid"].payload == AnalysisRecord.from_json(row_json).lanes["beatgrid"].payload
        assert result.record.lanes["beatgrid"].payload["bpm"] == (120.0 if untrusted else 99.0)
        assert result.record.features_blob.get("retained_activations") == [0.1, 0.2]
    else:
        _commit_through_queue(db, incoming, tmp_path)

    from apps.analysis import queue_store

    conn = open_conn(db)
    try:
        key_pointer = canonical_pointer(conn, sid, "key")
        # The cascade's stale marker: the refused-grid defect writes one here.
        key_marked = queue_store.stale_rows(conn, sid, "key")
    finally:
        conn.close()
    if untrusted:
        assert key_pointer is not None, "a refused grid staled the key that depends on the kept one"
        assert key_marked == set()
    else:
        assert key_pointer is None, "control: a trusted grid change still stales the dependent key"
