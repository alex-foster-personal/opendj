"""Phase 4 SYNC-05/06: sync_diff CSV emitter tests."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from apps.audit.sync_diff import (
    DiffRow,
    build_analysis_diff,
    iter_match_pairs,
    summarise,
    write_diff_csv,
)
from apps.shared.normalised import NormalisedAnalysis

pytestmark = [pytest.mark.requirement("SYNC-05"), pytest.mark.requirement("SYNC-06")]


def _write_matches(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "matches.csv"
    with p.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return p


def test_iter_match_pairs_filters_low_confidence(tmp_path: Path):
    path = _write_matches(
        tmp_path,
        [
            {"rb_content_id": "1", "djay_uuid": "a", "confidence": "0.9"},
            {"rb_content_id": "2", "djay_uuid": "b", "confidence": "0.5"},
        ],
    )
    assert list(iter_match_pairs(path, min_confidence=0.70)) == [("1", "a")]


def test_iter_match_pairs_handles_missing_ids(tmp_path: Path):
    path = _write_matches(
        tmp_path,
        [
            {"rb_content_id": "", "djay_uuid": "a", "confidence": "0.9"},
            {"rb_content_id": "1", "djay_uuid": "", "confidence": "0.9"},
            {"rb_content_id": "2", "djay_uuid": "b", "confidence": "0.9"},
        ],
    )
    assert list(iter_match_pairs(path)) == [("2", "b")]


def test_build_analysis_diff_matches_rating_delta():
    rb_an = {"1": NormalisedAnalysis(uuid_or_id="1", source="rb", bpm=128.0)}
    dj_an = {"a": NormalisedAnalysis(uuid_or_id="a", source="djay", bpm=130.0)}
    ans, ratings = build_analysis_diff(rb_an, dj_an, {"1": 5}, {"a": 3}, [("1", "a")])
    assert any(r.field == "rating" for r in ratings)
    assert any(r.field == "bpm" for r in ans)


def test_build_analysis_diff_skip_no_values():
    ans, ratings = build_analysis_diff({}, {}, {}, {}, [("1", "a")])
    assert ans == [] and ratings == []


def test_write_diff_csv_round_trip(tmp_path: Path):
    rows = [
        DiffRow(
            rb_content_id="1",
            djay_uuid="a",
            field="bpm",
            rb_value="128.0",
            djay_value="130.0",
            resolution="accept_rb",
            action_hint="write RB -> djay",
        )
    ]
    out = tmp_path / "analysis-diff.csv"
    write_diff_csv(rows, out)
    with out.open() as fp:
        records = list(csv.DictReader(fp))
    assert records[0]["field"] == "bpm"


def test_summarise_counts_per_field():
    rows = [
        DiffRow("1", "a", "bpm", "1", "2", "accept_rb", ""),
        DiffRow("2", "b", "bpm", "1", "3", "accept_rb", ""),
        DiffRow("3", "c", "rating", "0", "5", "accept_djay", ""),
    ]
    out = summarise(rows)
    assert out["bpm"] == {"accept_rb": 2}
    assert out["rating"] == {"accept_djay": 1}


def test_rating_zero_rule_visible_in_diff():
    _ans, ratings = build_analysis_diff({}, {}, {"1": 0}, {"a": 3}, [("1", "a")])
    assert any(r.resolution == "accept_djay" and r.field == "rating" for r in ratings)


def test_rb_side_missing_value_accepts_djay():
    rb_an = {}
    dj_an = {"a": NormalisedAnalysis(uuid_or_id="a", source="djay", bpm=128.0)}
    ans, _ = build_analysis_diff(rb_an, dj_an, {}, {}, [("1", "a")])
    assert any(r.resolution == "accept_djay" and r.field == "bpm" for r in ans)


def test_identical_values_no_change():
    rb_an = {"1": NormalisedAnalysis(uuid_or_id="1", source="rb", bpm=128.0)}
    dj_an = {"a": NormalisedAnalysis(uuid_or_id="a", source="djay", bpm=128.0)}
    ans, _ = build_analysis_diff(rb_an, dj_an, {}, {}, [("1", "a")])
    assert all(r.resolution == "no_change" for r in ans)


def test_prefer_rb_overrides_default():
    rb_an = {"1": NormalisedAnalysis(uuid_or_id="1", source="rb", bpm=128.0)}
    dj_an = {"a": NormalisedAnalysis(uuid_or_id="a", source="djay", bpm=130.0)}
    ans, _ = build_analysis_diff(rb_an, dj_an, {}, {}, [("1", "a")], prefer="rb")
    assert all(r.resolution == "accept_rb" for r in ans if r.field == "bpm")


def test_unknown_field_resolution_hint_mapped():
    rows = [DiffRow("1", "a", "foo", "x", "y", "conflict", "manual review")]
    out = summarise(rows)
    assert out["foo"]["conflict"] == 1
