"""Tests for apps.sync.diff_csv and djay_sync_service dry-run apply paths."""

from __future__ import annotations

import csv
from pathlib import Path

from apps.sync.diff_csv import load_diff_rows
from apps.sync.djay_sync_service import (
    run_analysis_apply,
    run_cues_apply,
    run_ratings_apply,
)

CUE_FIELDS = [
    "rb_content_id",
    "djay_uuid",
    "rb_cue_count",
    "djay_cue_count",
    "rb_only_positions",
    "djay_only_positions",
    "conflicting_positions",
    "union_count",
]

FIELD_DIFF_COLUMNS = [
    "rb_content_id",
    "djay_uuid",
    "field",
    "rb_value",
    "djay_value",
    "resolution",
    "action_hint",
]


def _write_csv(path: Path, fieldnames: list[str], row: dict[str, str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)


def test_load_diff_rows_missing_path_returns_empty(tmp_path: Path) -> None:
    """[if] diff CSV path absent [then] load_diff_rows returns [] [else stop]."""
    assert load_diff_rows(tmp_path / "nope.csv") == []


def test_load_diff_rows_reads_utf8_csv(tmp_path: Path) -> None:
    """[if] UTF-8 diff CSV on disk [then] row dict matches cells [else stop]."""
    path = tmp_path / "sample.csv"
    row = {
        "rb_content_id": "1",
        "djay_uuid": "uuid-a",
        "field": "rating",
        "rb_value": "5",
        "djay_value": "4",
        "resolution": "accept_rb",
        "action_hint": "write_rb",
    }
    _write_csv(path, FIELD_DIFF_COLUMNS, row)
    loaded = load_diff_rows(path)
    assert len(loaded) == 1
    assert loaded[0] == row


def test_run_cues_apply_dry_run_reads_csv(tmp_path: Path) -> None:
    """[if] cue diff CSV with one row [then] dry_run row_count is 1 [else stop]."""
    path = tmp_path / "cue-diff.csv"
    row = {
        "rb_content_id": "10",
        "djay_uuid": "u1",
        "rb_cue_count": "1",
        "djay_cue_count": "1",
        "rb_only_positions": "",
        "djay_only_positions": "",
        "conflicting_positions": "",
        "union_count": "1",
    }
    _write_csv(path, CUE_FIELDS, row)
    result = run_cues_apply(dry_run=True, diff_csv=path)
    assert result["dry_run"] is True
    assert result["row_count"] == 1


def test_run_cues_apply_dry_run_missing_csv(tmp_path: Path) -> None:
    """[if] cue diff path missing [then] dry_run row_count is 0 [else stop]."""
    result = run_cues_apply(dry_run=True, diff_csv=tmp_path / "missing.csv")
    assert result["dry_run"] is True
    assert result["row_count"] == 0


def test_run_analysis_apply_dry_run_reads_csv(tmp_path: Path) -> None:
    """[if] analysis diff CSV with one row [then] dry_run row_count is 1 [else stop]."""
    path = tmp_path / "analysis-diff.csv"
    row = {
        "rb_content_id": "2",
        "djay_uuid": "u2",
        "field": "bpm",
        "rb_value": "128",
        "djay_value": "128",
        "resolution": "no_change",
        "action_hint": "",
    }
    _write_csv(path, FIELD_DIFF_COLUMNS, row)
    result = run_analysis_apply(dry_run=True, diff_csv=path)
    assert result["dry_run"] is True
    assert result["row_count"] == 1


def test_run_ratings_apply_dry_run_reads_csv(tmp_path: Path) -> None:
    """[if] ratings diff CSV with one row [then] dry_run row_count is 1 [else stop]."""
    path = tmp_path / "ratings-diff.csv"
    row = {
        "rb_content_id": "3",
        "djay_uuid": "u3",
        "field": "rating",
        "rb_value": "5",
        "djay_value": "5",
        "resolution": "no_change",
        "action_hint": "",
    }
    _write_csv(path, FIELD_DIFF_COLUMNS, row)
    result = run_ratings_apply(dry_run=True, diff_csv=path)
    assert result["dry_run"] is True
    assert result["row_count"] == 1
