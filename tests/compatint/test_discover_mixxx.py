"""Mixxx archive discovery and coverage ledger (COMPATINT-11298).

[if] discovery runs on the captured Mixxx archive index [then] coverage and gaps are recorded, [else stop].
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from apps.compatint.coverage import format_mixxx_discovery_coverage_line, render_discovery_report
from apps.compatint.discover_mixxx import discover_mixxx_from_archive_index

pytestmark = pytest.mark.requirement("COMPATINT-11298")

_FIXTURE = Path(__file__).parent / "fixtures" / "mixxx_archive_index.html"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _result():
    html = _FIXTURE.read_text(encoding="utf-8")
    tags = ["2.5.5", "2.6-beta", "release-2.4.0"]
    return discover_mixxx_from_archive_index(html, tags=tags)


def test_discovery_runs_against_captured_archive_index() -> None:
    """[if] Mixxx discovery runs on the captured archive index [then] it records the archive source, [else stop]."""
    result = _result()
    assert "downloads.mixxx.org" in result.source


def test_stable_mixxx_release_listed_from_archive() -> None:
    """[if] a stable Mixxx release is in the archive index [then] it is recorded with URL and date, [else stop]."""
    result = _result()
    row = next(r for r in result.stable_releases if r.identifier == "2.5.6")
    assert row.primary_url == "https://downloads.mixxx.org/releases/2.5.6/"
    assert row.released_on == "2026-03-25"
    assert row.platforms == ["linux", "macos", "windows"]


def test_tag_without_archive_entry_is_gap() -> None:
    """[if] a Mixxx tag has no stable archive folder [then] it is listed in gaps, [else stop]."""
    result = _result()
    gap_ids = {gap.identifier for gap in result.gaps}
    assert "2.5.5" in gap_ids
    assert "2.4-beta" not in gap_ids


def test_report_prints_discovery_coverage_fraction() -> None:
    """[if] discovery finishes [then] the report prints acquired/denominator coverage, [else stop]."""
    result = _result()
    line = format_mixxx_discovery_coverage_line(result)
    assert line == "Mixxx discovery coverage: 4/4 (discovered set; not exhaustive)"
    report = render_discovery_report(result)
    assert "Mixxx discovery coverage: 4/4" in report


def test_corpus_json_marks_non_exhaustive_discovered_set() -> None:
    """[if] corpus JSON is built [then] list_kind is discovered and exhaustive is false, [else stop]."""
    corpus = _result().to_corpus_dict()
    assert corpus["list_kind"] == "discovered"
    assert corpus["exhaustive"] is False


def test_cli_discover_mixxx_prints_report_from_fixture() -> None:
    """[if] the discover-mixxx CLI runs on the fixture [then] stdout carries coverage text, [else stop]."""
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.compatint",
            "discover-mixxx",
            "--archive",
            str(_FIXTURE),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "Mixxx discovery coverage: 4/4" in done.stdout
