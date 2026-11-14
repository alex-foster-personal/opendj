"""#12485: every proposal names the evidence its in-window came from (cue, phrase, analysed section, time fallback)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from apps.autodj.propose import propose_transition
from apps.autodj.windows import mix_windows
from tests.autodj.fixtures import profile

_REQ = "TRANSAI-14365"
OUT, IN = profile("trk-out"), profile("trk-in", key="9A")


def _sources(in_profile) -> tuple[set[str], list[str]]:
    result = propose_transition(OUT, in_profile, out_deck=1, in_deck=2, intent="smooth", top=None)
    proposals = result["proposals"]
    assert proposals
    return {window.source for window in mix_windows(in_profile, "in")}, [p["rationale"] for p in proposals]


@pytest.mark.requirement(_REQ)
def test_transition_in_cue_source() -> None:
    """[if] a track has a transition-in cue [then] its window and every rationale name the transition-in cue, [else stop]."""
    sources, rationales = _sources(replace(IN, transition_in_bar=17))
    assert sources == {"transition_in_cue"}
    assert all("in-window from transition in cue" in r for r in rationales)


@pytest.mark.requirement(_REQ)
def test_phrase_cue_source() -> None:
    """[if] a track has no transition-in cue but rekordbox phrases [then] the rationale names the phrase cue, [else stop]."""
    sources, rationales = _sources(IN)
    assert sources == {"phrase_cue"}
    assert all("in-window from phrase cue" in r for r in rationales)


@pytest.mark.requirement(_REQ)
def test_analyzed_section_source() -> None:
    """[if] a track has no cue but its own analysed sections [then] the rationale names the analysed section, [else stop]."""
    sources, rationales = _sources(replace(IN, sections_source="own"))
    assert sources == {"analyzed_section"}
    assert all("in-window from analyzed section" in r for r in rationales)


@pytest.mark.requirement(_REQ)
def test_time_fallback_source() -> None:
    """[if] a track has no cue and no sections [then] the window and rationale name the time fallback, [else stop]."""
    sources, rationales = _sources(replace(IN, sections=(), sections_source="none"))
    assert sources == {"time_fallback"}
    assert all("in-window from time fallback" in r for r in rationales)
