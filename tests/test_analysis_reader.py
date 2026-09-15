"""Phase 4 SYNC-05: analysis-metadata reader parity tests.

We test the key-index Camelot mapping and the TSAF bool marker detector
at unit level since the full ``iter_analysis`` functions require live DB
fixtures that aren't shipped in this phase.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.djay_db import (
    _DJAY_KEY_IDX_TO_STD,
    _djay_key_idx_to_camelot,
    _extract_bool_marker,
)
from apps.shared.djay_db import (
    iter_analysis as djay_iter_analysis,
)
from apps.shared.normalised import NormalisedAnalysis

pytestmark = pytest.mark.requirement("SYNC-05")


def test_key_map_covers_twelve_majors_and_minors():
    majors = [v for k, v in _DJAY_KEY_IDX_TO_STD.items() if 0 <= k < 12]
    minors = [v for k, v in _DJAY_KEY_IDX_TO_STD.items() if 12 <= k < 24]
    assert len(majors) == 12
    assert len(minors) == 12
    assert all(v.endswith("m") for v in minors)


def test_c_major_maps_to_8b():
    assert _djay_key_idx_to_camelot(0) == "8B"


def test_a_minor_maps_to_8a():
    # A minor idx = 21 (Abm..Am order 20..21)
    assert _djay_key_idx_to_camelot(21) == "8A"


def test_unknown_key_idx_returns_none():
    assert _djay_key_idx_to_camelot(99) is None
    assert _djay_key_idx_to_camelot(None) is None


def test_bool_marker_false_detected():
    blob = b"TSAF" + b"\x00" * 12 + b"\x0d\x08isStraightGrid\x00" + b"\x00"
    assert _extract_bool_marker(blob, b"isStraightGrid") is False


def test_bool_marker_absent_returns_none():
    blob = b"TSAF" + b"\x00" * 16
    assert _extract_bool_marker(blob, b"isStraightGrid") is None


def test_bool_marker_true_when_no_false_byte():
    blob = b"TSAF" + b"\x00" * 12 + b"\x01\x08isStraightGrid\x00"
    assert _extract_bool_marker(blob, b"isStraightGrid") is True


def _make_empty_djay_db(tmp_path: Path) -> Path:
    """Minimum schema: database2 table + mediaItemAnalyzedData index table."""
    p = tmp_path / "media.db"
    con = sqlite3.connect(str(p))
    con.execute(
        "CREATE TABLE database2 (collection TEXT, key TEXT, data BLOB)"
    )
    con.execute(
        "CREATE TABLE secondaryIndex_mediaItemAnalyzedDataIndex ("
        " uuid TEXT PRIMARY KEY, bpm REAL, manualBPM REAL, "
        " keySignatureIndex INTEGER, isStraightGrid INTEGER)"
    )
    con.commit()
    con.close()
    return p


def test_iter_analysis_via_secondary_index(tmp_path: Path):
    p = _make_empty_djay_db(tmp_path)
    con = sqlite3.connect(str(p))
    con.execute(
        "INSERT INTO secondaryIndex_mediaItemAnalyzedDataIndex VALUES (?,?,?,?,?)",
        ("uuid-1", 128.0, 128.5, 0, 1),
    )
    con.commit()
    con.close()
    out = list(djay_iter_analysis(p))
    assert len(out) == 1
    a = out[0]
    assert isinstance(a, NormalisedAnalysis)
    assert a.uuid_or_id == "uuid-1"
    assert a.bpm == pytest.approx(128.0)
    assert a.manual_bpm == pytest.approx(128.5)
    assert a.key_camelot == "8B"
    assert a.is_straight_grid is True


def test_iter_analysis_fallback_scan(tmp_path: Path):
    """When secondary index is missing, fall back to TSAF blob scan."""
    p = tmp_path / "media.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE database2 (collection TEXT, key TEXT, data BLOB)")
    import struct

    blob = (
        b"TSAF" + b"\x00" * 12
        + b"\x13\x00\x00\x00" + struct.pack("<f", 120.0) + b"\x08bpm\x00"
        + b"\x08uuid-a\x00"
    )
    con.execute(
        "INSERT INTO database2 VALUES ('mediaItemAnalyzedData', 'uuid-a', ?)",
        (blob,),
    )
    con.execute(
        "INSERT INTO database2 VALUES ('mediaItemUserData', 'uuid-a', ?)",
        (b"TSAF" + b"\x00" * 12 + b"\x08uuid-a\x00",),
    )
    con.commit()
    con.close()

    out = list(djay_iter_analysis(p))
    assert len(out) >= 1
    by_uuid = {a.uuid_or_id: a for a in out}
    assert by_uuid.get("uuid-a")
    a = by_uuid["uuid-a"]
    assert a.bpm == pytest.approx(120.0, abs=0.01)


def test_iter_analysis_index_without_uuid_falls_back_to_blob_scan(tmp_path: Path):
    """[if] the analysed-data index table has no uuid column [then] the TSAF blob
    scan runs instead of a SELECT on a missing column, [else stop]. (#3036)"""
    import struct

    p = tmp_path / "media.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE database2 (collection TEXT, key TEXT, data BLOB)")
    con.execute(
        "CREATE TABLE secondaryIndex_mediaItemAnalyzedDataIndex ("
        " bpm REAL, keySignatureIndex INTEGER)"
    )
    blob = (
        b"TSAF" + b"\x00" * 12
        + b"\x13\x00\x00\x00" + struct.pack("<f", 120.0) + b"\x08bpm\x00"
        + b"\x08uuid-a\x00"
    )
    con.execute(
        "INSERT INTO database2 VALUES ('mediaItemAnalyzedData', 'uuid-a', ?)",
        (blob,),
    )
    con.execute(
        "INSERT INTO database2 VALUES ('mediaItemUserData', 'uuid-a', ?)",
        (b"TSAF" + b"\x00" * 12 + b"\x08uuid-a\x00",),
    )
    con.commit()
    con.close()

    out = list(djay_iter_analysis(p))
    assert len(out) >= 1
    assert out[0].uuid_or_id == "uuid-a"
