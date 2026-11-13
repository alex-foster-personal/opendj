"""CLI dry-run, CSV resolution, and Camelot key-index helpers.

[if] apply_analysis CLI dry-run or CSV flags change [then] stdout and exit codes match SYNC-05, [else stop].
"""

from __future__ import annotations

import pytest
from pathlib import Path

from apps.sync.analysis_csv_live import _camelot_to_djay_key_idx
from apps.sync.apply_analysis import _csv_resolution_summary, main
from tests.apply_analysis_support import (
    _make_diff,
    _owned_rollout_directory,  # noqa: F401 (autouse fixture, scoped to this module by the import)
)


pytestmark = [pytest.mark.requirement("SYNC-05"), pytest.mark.rekordbox_writeback]


def test_camelot_to_djay_key_8b_is_c_major():
    assert _camelot_to_djay_key_idx("8B") == 0


def test_camelot_to_djay_key_8a_is_a_minor():
    assert _camelot_to_djay_key_idx("8A") == 21


def test_camelot_to_djay_key_invalid_returns_none():
    assert _camelot_to_djay_key_idx("XX") is None


def test_dry_run_empty_rows(capsys):
    assert _csv_resolution_summary([]) == 0
    assert "no rows" in capsys.readouterr().out


def test_dry_run_summary_per_field(capsys):
    rows = [
        {"field": "bpm", "resolution": "accept_rb"},
        {"field": "bpm", "resolution": "accept_djay"},
        {"field": "tags", "resolution": "no_change"},
    ]
    _csv_resolution_summary(rows)
    out = capsys.readouterr().out
    assert "bpm:" in out
    assert "tags:" in out


def test_main_default_is_dry_run(tmp_path: Path, capsys):
    path = _make_diff(
        tmp_path,
        [
            {
                "rb_content_id": "1",
                "djay_uuid": "a",
                "field": "bpm",
                "rb_value": "128.0",
                "djay_value": "130.0",
                "resolution": "accept_rb",
                "action_hint": "write RB -> djay",
            }
        ],
    )
    assert main(["--diff-csv", str(path)]) == 0
    assert "bpm:" in capsys.readouterr().out


def test_main_bulk_without_flag_aborts(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    assert main(["--diff-csv", str(path), "--live", "--bulk"]) == 2


def test_main_fields_filter_respected(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    assert main(["--diff-csv", str(path), "--fields", "bpm,key_camelot"]) == 0


def test_camelot_round_trip_for_all_keys():
    from apps.shared.djay_db import _DJAY_KEY_IDX_TO_STD
    from apps.shared.harmonic import key_to_camelot

    for _idx, std in _DJAY_KEY_IDX_TO_STD.items():
        camelot = str(key_to_camelot(std))
        back = _camelot_to_djay_key_idx(camelot)
        assert back is not None
        assert str(key_to_camelot(_DJAY_KEY_IDX_TO_STD[back])) == camelot


def test_main_missing_csv_is_dry_run(tmp_path: Path, capsys):
    assert main(["--diff-csv", str(tmp_path / "nope.csv")]) == 0
