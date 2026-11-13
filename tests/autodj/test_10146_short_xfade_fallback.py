"""An unsynced 2-bar crossfade is offered as a ranked fallback when a grid is untrusted (#10146, spec 05 s3)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from apps.autodj.plan import parse_plan, validate_plan
from apps.autodj.profile import TrackProfile
from apps.autodj.propose import propose_transition
from tests.autodj.fixtures import profile

REQ = "TRANSAI-13892"


def _pair(*, out_trusted: bool = False, in_trusted: bool = True) -> tuple[TrackProfile, TrackProfile]:
    out, incoming = profile("trk-out"), profile("trk-in", key="9A")
    return (
        replace(out, grid_trusted=out_trusted, grid_note=None if out_trusted else "static grid untrusted"),
        replace(incoming, grid_trusted=in_trusted, grid_note=None if in_trusted else "no beatgrid"),
    )


def _propose(out: TrackProfile, incoming: TrackProfile, **kwargs: Any) -> dict[str, Any]:
    return propose_transition(out, incoming, out_deck=1, in_deck=2, top=None, **kwargs)


def _fallbacks(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in result["proposals"] if row.get("fallback")]


@pytest.mark.requirement(REQ)
def test_an_untrusted_grid_appends_a_two_bar_unsynced_crossfade() -> None:
    """[if] the outgoing grid is untrusted [then] a 2-bar tempo-none crossfade with flat lows is offered, [else stop]."""
    for out, incoming in (_pair(), _pair(out_trusted=True, in_trusted=False)):
        rows = _fallbacks(_propose(out, incoming))
        assert len(rows) == 1
        row = rows[0]
        assert row["style"] == "crossfade" and row["bars"] == 2
        assert row["plan"]["tempo"]["mode"] == "none"
        assert all(e["value"] == 0.5 for e in row["plan"]["events"] if e.get("control") == "eq_low")
        assert not [e for e in row["plan"]["events"] if e.get("control") == "eq_low" and e["at_bar"] < 2]
        assert row["rationale"].startswith("fallback: grid untrusted (")


@pytest.mark.requirement(REQ)
def test_the_fallback_enters_after_silence_and_exits_on_a_vocal_free_phrase_start() -> None:
    """[if] phrases exist [then] entry is the first non-silent phrase and exit the latest vocal-free start, [else stop]."""
    out, incoming = _pair()
    first, *rest = incoming.sections
    incoming = replace(incoming, sections=(replace(first, energy=0.0), *rest))
    last_start = max(section.start_bar for section in out.sections)
    vocals = tuple(0.9 if bar == last_start else 0.0 for bar in range(1, out.bars + 1))
    out = replace(out, bar_vocals=vocals)
    plan = _fallbacks(_propose(out, incoming))[0]["plan"]
    assert plan["in"]["window"]["start_bar"] == rest[0].start_bar
    starts = sorted(s.start_bar for s in out.sections if s.start_bar <= out.bars - 2 and s.start_bar != last_start)
    assert plan["out"]["window"]["start_bar"] == starts[-1]
    bare_out, bare_in = replace(out, sections=(), bar_vocals=None), replace(incoming, sections=())
    bare = _fallbacks(_propose(bare_out, bare_in))[0]["plan"]
    expected = max(1, bare_out.bars - 32 + 1)
    assert bare["out"]["window"]["start_bar"] == expected - (expected - 1) % 8
    assert bare["in"]["window"]["start_bar"] == 1


@pytest.mark.requirement(REQ)
def test_the_fallback_is_ranked_with_the_others_never_alone() -> None:
    """[if] other styles validate on an untrusted pair [then] the fallback is one ranked row among them, [else stop]."""
    result = _propose(*_pair())
    assert len(result["proposals"]) > 1
    assert len(_fallbacks(result)) == 1
    scores = [row["score"] for row in result["proposals"]]
    assert len(scores) == len(result["_valid_proposal_scores"])


@pytest.mark.requirement(REQ)
def test_both_grids_trusted_leave_the_proposals_unchanged() -> None:
    """[if] both grids are trusted [then] no fallback is appended and no row carries a fallback key, [else stop]."""
    result = _propose(*_pair(out_trusted=True, in_trusted=True))
    assert result["proposals"]
    assert not any("fallback" in row for row in result["proposals"])


@pytest.mark.requirement(REQ)
def test_validate_plan_does_not_warn_of_drift_on_the_fallback() -> None:
    """[if] the fallback plan is validated [then] it is ok and has no decks-will-drift warning, [else stop]."""
    out, incoming = _pair()
    row = _fallbacks(_propose(out, incoming))[0]
    validation = validate_plan(parse_plan(row["plan"]), out, incoming)
    assert validation.ok
    assert not [w for w in validation.warnings if "drift" in w]
