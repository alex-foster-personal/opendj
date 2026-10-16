"""Drop, break and build moments from DJ-labeled sections (STRUCT-07).

[if] DJ-labeled sections are marked [then] each real drop gets one numbered moment after a buildup or withdrawn section, [else stop].

Acceptance lines:
  [if] the arc is intro, buildup, drop, breakdown, buildup, drop, outro [then]
       Drop 1 and Drop 2 sit on the two drop starts, a break on the breakdown
       start, a build on each buildup start ⛔️ a drop moment on a quiet section
  [if] a drop is cut in two [then] the second half is a continuation and the
       track still has one drop ⛔️ two drops counted for one
  [if] the track opens at full energy [then] that span is a drop with no moment
       ⛔️ a "drop" with nothing to drop back in from
  [if] a drop under 4 bars is followed by a withdrawn section [then] it is a
       fake drop with no number and the next drop is Drop 1 ⛔️ the fake taking
       the number
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from apps.analysis_structure.drop_events import mark_drops
from apps.analysis_structure.pipeline import SCHEMA as SIDECAR_SCHEMA
from apps.analysis_structure.pipeline import write_sidecar
from apps.analysis_structure.serve import merge_own_phrases

pytestmark = pytest.mark.requirement("STRUCT-07")

FULL, OUT, HALF = -2.0, -20.0, -9.0


def _sections(plan: list[tuple[str, float, int]]) -> list[dict[str, Any]]:
    out, bar = [], 0
    for label, score, bars in plan:
        out.append(
            {
                "start_s": bar * 2.0,
                "end_s": (bar + bars) * 2.0,
                "start_bar": bar,
                "bars": bars,
                "dj_label": label,
                "energy": {"score_db": score},
            }
        )
        bar += bars
    return out


def _drops(moments: list[dict[str, Any]]) -> list[tuple[int, int | None, bool]]:
    return [(m["bar"], m["number"], m["fake"]) for m in moments if m["kind"] == "drop"]


def test_classic_arc_has_two_numbered_drops_a_break_and_two_builds():
    sections = _sections(
        [
            ("intro", OUT, 16), ("buildup", HALF, 16), ("drop", FULL, 32),
            ("breakdown", OUT, 16), ("buildup", HALF, 16), ("drop", FULL, 32), ("outro", OUT, 16),
        ]
    )  # fmt: skip
    moments = mark_drops(sections)
    assert _drops(moments) == [(32, 1, False), (96, 2, False)]
    assert [m["bar"] for m in moments if m["kind"] == "break"] == [64]
    assert [m["bar"] for m in moments if m["kind"] == "build"] == [16, 80]
    assert [m["time_s"] for m in moments] == sorted(m["time_s"] for m in moments)
    assert not any(s.get("continuation") for s in sections)


def test_a_drop_cut_in_two_is_one_drop_with_a_continuation():
    sections = _sections([("intro", OUT, 16), ("drop", FULL, 16), ("drop", FULL, 16), ("outro", OUT, 16)])
    moments = mark_drops(sections)
    assert _drops(moments) == [(16, 1, False)]
    assert [s.get("continuation") for s in sections] == [None, False, True, None]


def test_a_track_that_opens_full_has_no_drop_moment_there():
    sections = _sections([("drop", FULL, 32), ("breakdown", OUT, 16), ("drop", FULL, 32)])
    moments = mark_drops(sections)
    assert _drops(moments) == [(48, 1, False)]


def test_a_drop_after_an_intro_that_never_pulled_out_has_no_moment():
    """Control for the contrast rule: it must be able to say no."""
    sections = _sections([("intro", HALF, 16), ("drop", FULL, 32), ("outro", OUT, 16)])
    assert _drops(mark_drops(sections)) == []
    # Same arc with a withdrawn intro: the moment fires.
    sections = _sections([("intro", OUT, 16), ("drop", FULL, 32), ("outro", OUT, 16)])
    assert _drops(mark_drops(sections)) == [(16, 1, False)]


def test_a_short_drop_before_a_withdrawn_section_is_fake_and_unnumbered():
    sections = _sections(
        [
            ("intro", OUT, 16), ("drop", FULL, 2), ("breakdown", OUT, 14),
            ("buildup", HALF, 16), ("drop", FULL, 32),
        ]
    )  # fmt: skip
    assert _drops(mark_drops(sections)) == [(16, None, True), (48, 1, False)]
    # Control: the same 2-bar drop followed by a full section is real.
    sections = _sections([("intro", OUT, 16), ("drop", FULL, 2), ("drop", FULL, 30)])
    assert _drops(mark_drops(sections)) == [(16, 1, False)]


def test_a_short_drop_cut_in_two_before_a_withdrawn_section_is_one_fake_drop():
    """Rule 3 sees the whole contiguous drop, not its first piece (discussion_r4264558047)."""
    sections = _sections(
        [
            ("intro", OUT, 16), ("drop", FULL, 2), ("drop", FULL, 1), ("breakdown", OUT, 13),
            ("buildup", HALF, 16), ("drop", FULL, 32),
        ]
    )  # fmt: skip
    assert _drops(mark_drops(sections)) == [(16, None, True), (48, 1, False)]
    assert sections[2]["continuation"] is True, "control: the 1-bar piece is the same drop"
    # Control: pieces that add up to FAKE_MAX_BARS or more are a real drop.
    sections = _sections(
        [("intro", OUT, 16), ("drop", FULL, 2), ("drop", FULL, 2), ("breakdown", OUT, 12)]
    )  # fmt: skip
    assert _drops(mark_drops(sections)) == [(16, 1, False)]


def test_unlabeled_sections_have_no_moments():
    sections = [{"start_s": 0.0, "end_s": 30.0, "label": "inst"}]
    assert mark_drops(sections) == []


# The first drop is cut in two, so the second half must come back as a continuation.
LEGACY_PLAN = [
    ("intro", OUT, 16), ("buildup", HALF, 16), ("drop", FULL, 16), ("drop", FULL, 16),
    ("breakdown", OUT, 16), ("buildup", HALF, 16), ("drop", FULL, 32), ("outro", OUT, 16),
]  # fmt: skip


def _all_in_one_doc(sections: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "schema": SIDECAR_SCHEMA,
        "stable_id": "a" * 40,
        "status": "ok",
        "producer": "allin1",
        "sections": [{**s, "label": s["dj_label"]} for s in sections],
        **extra,
    }


def test_a_pre_struct_07_all_in_one_sidecar_gets_its_moments_on_load(tmp_path: Path):
    """A sidecar written before STRUCT-07 has no ``moments`` key: derive them, never serve []."""
    sections = _sections(LEGACY_PLAN)
    write_sidecar(tmp_path, _all_in_one_doc(sections))
    out = merge_own_phrases({"phrases": []}, "a" * 40, tmp_path, source="own")
    assert out["phrases_source"] == "own"
    assert _drops(out["phrase_moments"]) == [(32, 1, False), (96, 2, False)]
    assert [p.get("continuation", False) for p in out["phrases"]] == [
        False,
        False,
        False,
        True,
        False,
        False,
        False,
        False,
    ]
    assert len(sections) == 8


def test_stored_moments_that_disagree_with_the_sections_are_never_served(tmp_path: Path):
    """Moments are derived from the sections on every load, so stored ones can never contradict the spans."""
    write_sidecar(tmp_path, _all_in_one_doc(_sections(LEGACY_PLAN), moments=[]))
    out = merge_own_phrases({"phrases": []}, "a" * 40, tmp_path, source="own")
    assert _drops(out["phrase_moments"]) == [(32, 1, False), (96, 2, False)]


def test_a_section_with_no_start_bar_key_fails_instead_of_a_barless_moment():
    sections = _sections([("intro", OUT, 16), ("drop", FULL, 16)])
    del sections[1]["start_bar"]
    with pytest.raises(KeyError):
        mark_drops(sections)
    # Control: an explicit None (All-In-One's lead section at 0 s) is a documented value, not malformed.
    lead = _sections([("buildup", HALF, 16), ("drop", FULL, 16)])
    lead[0]["start_bar"] = None
    assert [m["bar"] for m in mark_drops(lead)] == [None, 16]


def test_a_continuation_never_gets_a_moment_even_after_a_withdrawn_scoring_drop():
    """Rule 1 is unconditional: a drop span scored at or below WITHDRAWN_DB still does not start a new drop."""
    sections = _sections([("buildup", HALF, 16), ("drop", OUT, 16), ("drop", FULL, 16)])
    moments = mark_drops(sections)
    assert sections[2]["continuation"] is True
    assert _drops(moments) == [(16, 1, False)], "one drop, not a second one on its continuation"
