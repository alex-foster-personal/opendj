"""Set target rules (LIBUX-8679): played length, Camelot clashes, skip bridging, readout.

- [if] a track has no mix points [then] played length is duration minus a 32-bar overlap, clamped at 50 %, [else stop].
- [if] a track has no BPM [then] played length is 75 % of its duration, [else stop].
- [if] two adjacent keys are same code, +-1 same letter, or same number other letter [then] no clash, [else stop].
- [if] keys are 3+ steps apart, or a letter change with 2+ steps [then] the clash is gross, [else stop].
- [if] a skipped track sits between two keys [then] the clash compares the tracks either side, [else stop].
- [if] a key is unknown [then] no clash is reported, [else stop].
- [if] the set is over or under target [then] the readout says "Xm over" or "Xm left", [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.set_target import SetEntry, format_readout, key_clash, played_length_s, summarize_set

pytestmark = pytest.mark.requirement("LIBUX-8679")

CASES = json.loads((Path(__file__).resolve().parents[2] / "apps/shared/set_target/parity_cases.json").read_text())


@pytest.mark.parametrize("case", CASES["played"])
def test_played_length_matches_parity_cases(case: dict) -> None:
    got = played_length_s(case["duration_s"], case["bpm"], case.get("mix_in_s"), case.get("mix_out_s"))
    assert got == pytest.approx(case["expect"])


@pytest.mark.parametrize("case", CASES["clash"])
def test_key_clash_matches_parity_cases(case: dict) -> None:
    expect = None if case["expect"] is None else tuple(case["expect"])
    assert key_clash(case["from"], case["to"]) == expect


@pytest.mark.parametrize("case", CASES["summary"])
def test_summary_matches_parity_cases(case: dict) -> None:
    entries = [
        SetEntry(
            stable_id=e["stable_id"],
            duration_s=e["duration_s"],
            bpm=e["bpm"],
            key=e["key"],
            skipped=e.get("skipped", False),
            mix_in_s=e.get("mix_in_s"),
            mix_out_s=e.get("mix_out_s"),
        )
        for e in case["entries"]
    ]
    summary = summarize_set(entries, case["target_s"]).to_dict()
    expect = dict(case["expect"])
    readout = expect.pop("readout")
    assert summary == pytest.approx(expect)
    assert format_readout(summary["played_s"], case["target_s"]) == readout


def test_readout_over_target() -> None:
    assert format_readout(67 * 60, 3600) == "~67m of 60m · 7m over"
    assert format_readout(48 * 60, 3600) == "~48m of 60m · 12m left"


def test_skips_excluded_from_total_and_bridged() -> None:
    entries = [
        SetEntry("a", 300, 128, "8A"),
        SetEntry("b", 300, 128, "8A", skipped=True),
        SetEntry("c", 300, 128, "8A"),
    ]
    summary = summarize_set(entries, None)
    assert summary.played_s == pytest.approx(480)
    assert summary.skipped == 1 and summary.clashes == []
