"""SYNC-03: Phase 2 match-set CSV loader tests."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from apps.sync.playlist_plan import (
    CONFIRMED_CONFIDENCE,
    CONFIRMED_SIGNALS,
    load_match_set,
)


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    header = "rb_id,djay_uuid,confidence,signals_fired\n"
    body = "\n".join(
        f"{r['rb_id']},{r['djay_uuid']},{r['confidence']},{r['signals_fired']}"
        for r in rows
    )
    path.write_text(header + body + "\n", encoding="utf-8")


@pytest.mark.requirement("SYNC-03")
def test_match_set_filters_below_threshold(tmp_path: Path) -> None:
    csv_path = tmp_path / "matches.csv"
    _write_csv(
        csv_path,
        [
            {"rb_id": "1", "djay_uuid": "u1", "confidence": "0.95", "signals_fired": "4"},
            {"rb_id": "2", "djay_uuid": "u2", "confidence": "0.70", "signals_fired": "3"},
            {"rb_id": "3", "djay_uuid": "u3", "confidence": "0.72", "signals_fired": "3"},
            # Below threshold cases
            {"rb_id": "4", "djay_uuid": "u4", "confidence": "0.60", "signals_fired": "5"},
            {"rb_id": "5", "djay_uuid": "u5", "confidence": "0.99", "signals_fired": "2"},
        ],
    )
    m = load_match_set(csv_path)
    assert set(m.rb_to_djay.keys()) == {"1", "2", "3"}
    assert m.rb_to_djay["1"] == "u1"
    assert m.djay_to_rb["u2"] == "2"
    # Confirmed IDs helpers
    assert m.confirmed_rb_ids == {"1", "2", "3"}
    assert m.confirmed_djay_uuids == {"u1", "u2", "u3"}


@pytest.mark.requirement("SYNC-03")
def test_match_set_exposes_sha256_of_source(tmp_path: Path) -> None:
    csv_path = tmp_path / "matches.csv"
    rows = [
        {"rb_id": "1", "djay_uuid": "u1", "confidence": "0.9", "signals_fired": "3"},
    ]
    _write_csv(csv_path, rows)
    m = load_match_set(csv_path)
    expected = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    assert m.source_sha256 == expected
    assert m.source_path == str(csv_path)


@pytest.mark.requirement("SYNC-03")
def test_match_set_missing_file_raises_with_hint(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as exc:
        load_match_set(tmp_path / "no-such.csv")
    assert "matcher" in str(exc.value).lower()


@pytest.mark.requirement("SYNC-03")
def test_match_set_skips_rows_with_missing_ids(tmp_path: Path) -> None:
    csv_path = tmp_path / "matches.csv"
    _write_csv(
        csv_path,
        [
            {"rb_id": "", "djay_uuid": "u1", "confidence": "0.9", "signals_fired": "4"},
            {"rb_id": "2", "djay_uuid": "", "confidence": "0.9", "signals_fired": "4"},
            {"rb_id": "3", "djay_uuid": "u3", "confidence": "0.9", "signals_fired": "4"},
        ],
    )
    m = load_match_set(csv_path)
    assert m.rb_to_djay == {"3": "u3"}


@pytest.mark.requirement("SYNC-03")
def test_match_set_thresholds_exposed_as_module_constants() -> None:
    """The threshold values are fixed and equal to the Phase 2 contract."""
    assert CONFIRMED_CONFIDENCE == 0.70
    assert CONFIRMED_SIGNALS == 3


@pytest.mark.requirement("SYNC-03")
def test_match_set_handles_malformed_numeric_fields(tmp_path: Path) -> None:
    csv_path = tmp_path / "matches.csv"
    header = "rb_id,djay_uuid,confidence,signals_fired\n"
    body = "1,u1,not_a_float,3\n2,u2,0.9,abc\n3,u3,0.9,4\n"
    csv_path.write_text(header + body, encoding="utf-8")
    m = load_match_set(csv_path)
    assert m.rb_to_djay == {"3": "u3"}
