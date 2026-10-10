"""Sibling lanes follow the own projection of the same bytes (NATIVE-30).

[if] a sibling hash differs or the BPM is only rekordbox [then] it is not copied, [else stop].
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis import selection, store as analysis_store
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.selection import EffectiveField
from apps.webui.server.analysis_overlay import _borrow_path_sibling_lanes
from apps.webui.server.rb_vendor_pkg import own_beatgrid_overlay
from apps.webui.server.rb_vendor_pkg import own_key_overlay
from apps.webui.server.rb_vendor_pkg import own_waveform_overlay

pytestmark = pytest.mark.requirement("NATIVE-30")

STAMP = "2026-10-08T00:00:00Z"
FINGERPRINT = "sha256:" + "66" * 32
SAME = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32
PATH = "/music/track.wav"


@pytest.fixture(autouse=True)
def _toggles() -> None:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


def _db(tmp_path: Path):
    path = tmp_path / "state.db"
    conn = analysis_store.open_conn(path)
    return path, conn


def _track(conn, stable_id: str, digest: str, path: str = PATH) -> None:
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, "
        "audio_hash, created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, ?, ?)",
        (stable_id, stable_id, path, digest, STAMP, STAMP),
    )


def _missing(name: str = "bpm") -> dict[str, EffectiveField]:
    return {
        name: EffectiveField(
            value=None,
            source="own",
            confidence=None,
            modified_at="",
            status="missing",
            reason="no own analysis record for this lane yet",
        )
    }


def _projection(conn, stable_id: str, bpm: float) -> None:
    conn.execute(
        "INSERT INTO analysis_projection (stable_id, field, value, status, reason, "
        "confidence, backend, backend_version, updated_at) "
        "VALUES (?, 'bpm', ?, 'ok', NULL, 0.9, 'own_beatgrid.backfill', '1.6.0', ?)",
        (stable_id, bpm, STAMP),
    )


def _record(stable_id: str, lane: str, backend: str, version: str, reason: str) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=stable_id,
        backend=backend,
        backend_version=version,
        analyzed_at=datetime(2026, 10, 8, tzinfo=UTC),
        duration_s=30.0,
        sample_rate=44100,
        bpm=120.0,
        bpm_confidence=0.9,
        key_camelot="8A",
        key_openkey="1m",
        key_confidence=0.8,
        energy=5,
        producer="backfill",
        producer_version=version,
        uses_model=False,
        model_sha256=None,
        decode_fingerprint=FINGERPRINT,
        lanes={lane: LaneResult(status="failed", reason=reason, payload={})},
    )


def test_a_rekordbox_only_sibling_bpm_is_not_copied(tmp_path: Path) -> None:
    """[if] a sibling BPM lives only in track_fields [then] the own row stays missing, [else stop]."""
    _path, conn = _db(tmp_path)
    try:
        _track(conn, "target", SAME)
        _track(conn, "donor", SAME)
        _track(conn, "unrelated", OTHER, "/music/other.wav")
        _projection(conn, "unrelated", 100.0)
        conn.execute(
            "INSERT INTO track_fields (stable_id, field_name, value_json, source, "
            "confidence, modified_at) VALUES ('donor', 'bpm', '140.0', 'rekordbox', 1.0, ?)",
            (STAMP,),
        )
        conn.commit()
        fields = {"target": _missing()}
        borrowed = _borrow_path_sibling_lanes(conn, fields)
        assert borrowed["target"]["bpm"].status == "missing"
    finally:
        conn.close()


def test_a_same_hash_sibling_own_bpm_is_copied(tmp_path: Path) -> None:
    """[if] a same-hash sibling has an own BPM [then] the missing row copies that BPM, [else stop]."""
    _path, conn = _db(tmp_path)
    try:
        _track(conn, "target", SAME)
        _track(conn, "donor", SAME)
        _projection(conn, "donor", 128.0)
        conn.commit()
        fields = {"target": _missing()}
        borrowed = _borrow_path_sibling_lanes(conn, fields)
        assert borrowed["target"]["bpm"].status == "ok"
        assert borrowed["target"]["bpm"].value == 128.0
        assert borrowed["target"]["bpm"].source == "own_beatgrid.backfill"
    finally:
        conn.close()


def test_a_different_hash_sibling_bpm_is_not_copied(tmp_path: Path) -> None:
    """[if] a sibling audio hash differs [then] its own BPM is not copied, [else stop]."""
    _path, conn = _db(tmp_path)
    try:
        _track(conn, "target", SAME)
        _track(conn, "donor", OTHER)
        _projection(conn, "donor", 128.0)
        conn.commit()
        fields = {"target": _missing()}
        borrowed = _borrow_path_sibling_lanes(conn, fields)
        assert borrowed["target"]["bpm"].status == "missing"
    finally:
        conn.close()


def test_anlz_readers_borrow_only_a_same_hash_sibling(tmp_path: Path) -> None:
    """[if] a same-hash lane is served [then] a different hash stays missing, [else stop]."""
    path, conn = _db(tmp_path)
    try:
        _track(conn, "beat-target", SAME)
        _track(conn, "beat-donor", SAME)
        _track(conn, "key-target", SAME, "/music/key.wav")
        _track(conn, "key-donor", SAME, "/music/key.wav")
        _track(conn, "wave-target", OTHER, "/music/wave.wav")
        _track(conn, "wave-donor", SAME, "/music/wave.wav")
        conn.commit()
        analysis_store.upsert_record(
            _record("beat-donor", "beatgrid", "own_beatgrid.backfill", "1.6.0", "no_trackable_pulse"),
            db_path=path,
        )
        analysis_store.upsert_record(
            _record("key-donor", "key", "own_key.backfill", "1.1.0", "no_tonal_center"),
            db_path=path,
        )
        analysis_store.upsert_record(
            _record("wave-donor", "waveform", "own_waveform.backfill", "1.4.0", "not_decoded"),
            db_path=path,
        )
        selection.set_toggle("beatgrid", "own")
        selection.set_toggle("key", "own")
        selection.set_toggle("waveform", "own")
        beat = own_beatgrid_overlay.apply_own_beatgrid({}, "beat-target", path)["beatgrid"]
        key = own_key_overlay.apply_own_key_segments({}, "key-target", path)["key_segments"]
        wave = own_waveform_overlay.apply_own_waveform({}, "wave-target", path)["waveform"]
        assert beat["status"] == "failed" and beat["reason"] == "no_trackable_pulse"
        assert key["status"] == "failed" and key["reason"] == "no_tonal_center"
        assert wave["status"] == "missing"
    finally:
        conn.close()


def test_an_nfd_path_borrows_the_nfc_siblings_lane(tmp_path: Path) -> None:
    """[if] the sibling path is NFD [then] the NFC row borrows its lane, [else stop]."""
    import unicodedata

    from apps.analysis.byte_siblings import same_bytes_sibling_ids

    nfc = unicodedata.normalize("NFC", "caf" + chr(0xE9) + ".wav")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    _path, conn = _db(tmp_path)
    try:
        _track(conn, "donor", SAME, "/music/" + nfd)
        _track(conn, "target", SAME, "/music/" + nfc)
        conn.commit()
        assert same_bytes_sibling_ids(conn, "target") == ["donor"]
    finally:
        conn.close()


def test_an_nfd_donor_path_still_lends_its_bpm(tmp_path: Path) -> None:
    """[if] the donor path is NFD [then] the NFC row copies its BPM, [else stop]."""
    import unicodedata

    nfc = unicodedata.normalize("NFC", "/music/caf" + chr(0xE9) + ".wav")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    _path, conn = _db(tmp_path)
    try:
        _track(conn, "target", SAME, nfc)
        _track(conn, "donor", SAME, nfd)
        _projection(conn, "donor", 128.0)
        conn.commit()
        borrowed = _borrow_path_sibling_lanes(conn, {"target": _missing()})
        assert borrowed["target"]["bpm"].status == "ok"
        assert borrowed["target"]["bpm"].value == 128.0
    finally:
        conn.close()


def _ok_beatgrid(stable_id: str) -> AnalysisRecord:
    import dataclasses

    payload = {
        "beats": [
            {"t": 0.0, "n": 1, "bpm": 124.0},
            {"t": 0.48, "n": 2, "bpm": 124.0},
        ],
        "bpm": 124.0,
        "bpm_confidence": 0.9,
        "octave_reason": "single_octave_in_range",
        "first_downbeat_s": 0.0,
        "tempo_changes": [],
        "static_grid_untrusted": False,
    }
    failed = _record(
        stable_id, "beatgrid", "own_beatgrid.backfill", "1.6.0", "no_trackable_pulse"
    )
    return dataclasses.replace(
        failed,
        bpm=124.0,
        lanes={"beatgrid": LaneResult(status="ok", reason=None, payload=payload)},
    )


def test_two_donors_serve_the_same_ok_lane(tmp_path: Path) -> None:
    """[if] one sibling failed and a later one is ok [then] both surfaces serve the ok lane, [else stop]."""
    path, conn = _db(tmp_path)
    try:
        _track(conn, "target", SAME)
        _track(conn, "a-fail", SAME)
        _track(conn, "b-ok", SAME)
        conn.commit()
        analysis_store.upsert_record(
            _record(
                "a-fail", "beatgrid", "own_beatgrid.backfill", "1.6.0", "no_trackable_pulse"
            ),
            db_path=path,
        )
        analysis_store.upsert_record(_ok_beatgrid("b-ok"), db_path=path)
        selection.set_toggle("beatgrid", "own")
        borrowed = _borrow_path_sibling_lanes(conn, {"target": _missing()})
        beat = own_beatgrid_overlay.apply_own_beatgrid({}, "target", path)["beatgrid"]
        assert borrowed["target"]["bpm"].status == "ok"
        assert float(borrowed["target"]["bpm"].value) == 124.0
        assert beat["status"] == "ok"
        assert beat["bpm"] == 124.0
        assert beat["reason"] != "no_trackable_pulse"
    finally:
        conn.close()


def test_an_own_lane_does_not_mix_a_sibling_field(tmp_path: Path) -> None:
    """[if] the target already has its lane [then] a sibling field is not mixed in, [else stop]."""
    _path, conn = _db(tmp_path)
    try:
        _track(conn, "target", SAME)
        _track(conn, "donor", SAME)
        _projection(conn, "donor", 128.0)
        conn.execute(
            "INSERT INTO analysis_projection (stable_id, field, value, status, reason, "
            "confidence, backend, backend_version, updated_at) "
            "VALUES ('donor', 'tempo_change_count', '3', 'ok', NULL, 0.9, "
            "'own_beatgrid.backfill', '1.6.0', ?)",
            (STAMP,),
        )
        conn.commit()
        own = EffectiveField(
            value=100.0,
            source="own_beatgrid.backfill",
            confidence=0.9,
            modified_at=STAMP,
            status="ok",
            reason=None,
        )
        missing_count = EffectiveField(
            value=None,
            source="own",
            confidence=None,
            modified_at="",
            status="missing",
            reason="no own analysis record for this lane yet",
        )
        borrowed = _borrow_path_sibling_lanes(
            conn, {"target": {"bpm": own, "tempo_change_count": missing_count}}
        )
        assert borrowed["target"]["bpm"].value == 100.0
        assert borrowed["target"]["tempo_change_count"].status == "missing"
    finally:
        conn.close()


def test_an_unknown_lane_status_is_rejected() -> None:
    """[if] a donor status is unknown [then] the chooser raises, [else stop]."""
    from apps.analysis.byte_siblings import choose_lane_donor

    with pytest.raises(ValueError, match="unknown lane status"):
        choose_lane_donor([("row-a", "typo")])
    assert choose_lane_donor([("row-a", "failed")]) == "row-a"


def test_a_missing_target_lane_borrows_the_sibling_in_the_reader(tmp_path: Path) -> None:
    """[if] the target's own lane result is missing [then] the reader serves the sibling, [else stop]."""
    from apps.analysis.byte_siblings import canonical_result_or_same_bytes_sibling

    _path, conn = _db(tmp_path)
    try:
        _track(conn, "target", SAME)
        _track(conn, "a-fail", SAME)
        _track(conn, "b-ok", SAME)
        _track(conn, "lonely", OTHER, "/music/lonely.wav")
        conn.commit()
        missing = LaneResult(status="missing", reason="not analyzed yet", payload={})
        lonely = LaneResult(status="missing", reason="not analyzed yet", payload={})
        lanes = {
            "target": missing,
            "a-fail": LaneResult(status="failed", reason="no_trackable_pulse", payload={}),
            "b-ok": LaneResult(status="ok", reason=None, payload={"bpm": 124.0}),
            "lonely": lonely,
        }

        def lookup(_conn, stable_id: str, _lane: str):
            return lanes.get(stable_id)

        served = canonical_result_or_same_bytes_sibling(conn, "target", "beatgrid", lookup)
        assert served is lanes["b-ok"], "a missing target lane must not shadow an ok sibling"
        kept = canonical_result_or_same_bytes_sibling(conn, "lonely", "beatgrid", lookup)
        assert kept is lonely, "with no donor the target's own missing result is preserved"
        lanes["target"] = LaneResult(status="failed", reason="too_few_beats", payload={})
        own = canonical_result_or_same_bytes_sibling(conn, "target", "beatgrid", lookup)
        assert own is lanes["target"], "a real own lane, even a failure, is never replaced"
    finally:
        conn.close()


def test_an_unknown_status_after_an_ok_candidate_is_still_rejected() -> None:
    """[if] an ok donor precedes an unknown status [then] the chooser raises, [else stop]."""
    from apps.analysis.byte_siblings import choose_lane_donor

    with pytest.raises(ValueError, match="unknown lane status"):
        choose_lane_donor([("row-a", "ok"), ("row-b", "typo")])
    assert choose_lane_donor([("row-a", "ok"), ("row-b", "failed")]) == "row-a"


def test_a_projected_lane_with_one_ok_and_one_unknown_status_is_rejected() -> None:
    """[if] one projected field is ok and another is unknown [then] it raises, [else stop]."""
    from apps.webui.server.analysis_overlay import _donor_lane_status

    def view(status: str) -> EffectiveField:
        return EffectiveField(
            value=124.0, source="own", confidence=0.9, modified_at=STAMP,
            status=status, reason=None,  # type: ignore[arg-type]
        )

    names = ("bpm", "tempo_change_count")
    with pytest.raises(ValueError, match="unknown lane status 'typo'"):
        _donor_lane_status({"bpm": view("ok"), "tempo_change_count": view("typo")}, names)
    assert _donor_lane_status({"bpm": view("ok"), "tempo_change_count": view("failed")}, names) == "ok"
    assert _donor_lane_status({"bpm": view("missing"), "tempo_change_count": view("failed")}, names) == "failed"
    assert _donor_lane_status({"bpm": view("missing")}, names) is None
