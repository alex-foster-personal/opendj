"""Phase 4 SYNC-06: apply_ratings CLI unit tests (dry-run + planning)."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from apps.sync.apply_ratings import _summarise_plan, dry_run, main

pytestmark = pytest.mark.requirement("SYNC-06")


def _make_diff(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "ratings-diff.csv"
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "rb_content_id",
                "djay_uuid",
                "field",
                "rb_value",
                "djay_value",
                "resolution",
                "action_hint",
            ],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return path


def test_dry_run_empty_rows_returns_zero(capsys):
    assert dry_run([]) == 0
    assert "no rows" in capsys.readouterr().out


def test_dry_run_summary_counts_resolutions(capsys):
    rows = [
        {"resolution": "accept_rb"},
        {"resolution": "accept_rb"},
        {"resolution": "accept_djay"},
        {"resolution": "conflict"},
        {"resolution": "no_change"},
    ]
    dry_run(rows)
    out = capsys.readouterr().out
    assert "accept_rb: 2" in out
    assert "accept_djay: 1" in out
    assert "conflict: 1" in out


def test_summarise_plan_keys():
    counts = _summarise_plan(
        [{"resolution": "accept_rb"}, {"resolution": "accept_rb"}]
    )
    assert counts == {"accept_rb": 2}


def test_main_dry_run_reads_csv(tmp_path: Path, capsys):
    path = _make_diff(
        tmp_path,
        [
            {
                "rb_content_id": "1",
                "djay_uuid": "a",
                "field": "rating",
                "rb_value": "0",
                "djay_value": "5",
                "resolution": "accept_djay",
                "action_hint": "write djay -> RB",
            }
        ],
    )
    assert main(["--diff-csv", str(path)]) == 0
    assert "accept_djay: 1" in capsys.readouterr().out


def test_main_bulk_without_flag_aborts(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    assert main(["--diff-csv", str(path), "--live", "--bulk"]) == 2


def test_main_missing_csv_returns_empty_summary(tmp_path: Path, capsys):
    missing = tmp_path / "nope.csv"
    assert main(["--diff-csv", str(missing)]) == 0
    assert "no rows" in capsys.readouterr().out


def test_main_tracks_filter_dry_run(tmp_path: Path):
    path = _make_diff(
        tmp_path,
        [
            {
                "rb_content_id": "1",
                "djay_uuid": "a",
                "field": "rating",
                "rb_value": "5",
                "djay_value": "0",
                "resolution": "accept_rb",
                "action_hint": "",
            }
        ],
    )
    assert main(["--diff-csv", str(path), "--tracks", "a"]) == 0
