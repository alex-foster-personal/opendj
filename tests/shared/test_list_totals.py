"""List footer totals (LIBUX list footer): shared summarize and format.

[if] durations include nulls [then] unknown rows count toward the count but not the time, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.list_totals import (
    ListTotals,
    format_duration_hms,
    format_library_footer,
    format_list_totals_line,
    summarize_durations_ms,
)

pytestmark = pytest.mark.requirement("LIBUX-10716")

_FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "list_totals_cases.json").read_text())


@pytest.mark.parametrize("case", _FIXTURE["cases"], ids=lambda c: c["name"])
def test_fixture_cases(case: dict) -> None:
    """[if] fixture durations are summarized [then] count, sum and line match, [else stop]."""
    totals = summarize_durations_ms(case["durations_ms"])
    assert totals.to_dict() == case["totals"]
    assert format_list_totals_line(totals) == case["line"]


def test_footer_selection_suffix() -> None:
    """[if] two or more rows are selected [then] the footer adds the selected suffix, [else stop]."""
    f = _FIXTURE["footer"]
    text = format_library_footer(ListTotals(**f["list"]), ListTotals(**f["selection"]))
    assert text == f["text"]


def test_format_duration_hms() -> None:
    """[if] ms is converted [then] H:MM:SS or M:SS is used, [else stop]."""
    assert format_duration_hms(65_000) == "1:05"
    assert format_duration_hms(3_661_000) == "1:01:01"
