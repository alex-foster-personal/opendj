"""drop_swap honours the incoming track's transition-in cue, or is refused for the pair with the reason (#9719).

[if] the incoming track has a transition-in cue [then] drop_swap enters on it or is refused with the reason, [else stop].

Regression one-liners:
  - if drop_swap enters on a peak other than the transition-in cue then broken
  - if a cue that is not a peak start lets drop_swap through silently then broken
  - if a track without a cue loses today's drop_swap peak windows then broken
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from apps.autodj.profile import TrackProfile
from apps.autodj.propose import drop_swap_cue_refusal, propose_transition
from apps.autodj.sections import PEAK_LABELS
from tests.autodj.fixtures import profile

pytestmark = pytest.mark.requirement("AGENT-13793")

OUT, IN = profile("trk-out"), profile("trk-in", key="9A")


def _run(incoming: TrackProfile) -> dict[str, Any]:
    return propose_transition(OUT, incoming, out_deck=1, in_deck=2, intent="showy", top=None)


def test_cue_on_a_peak_start_is_the_only_drop_swap_entry() -> None:
    """[if] the cue starts a peak section [then] every drop_swap proposal enters on the cue bar, [else stop]."""
    result = _run(replace(IN, transition_in_bar=33))
    swaps = [row for row in result["proposals"] if row["style"] == "drop_swap"]
    assert swaps, result["not_offered"]
    assert {row["plan"]["in"]["window"]["start_bar"] for row in swaps} == {33}


def test_cue_off_a_peak_refuses_drop_swap_with_the_bar_and_label() -> None:
    """[if] the cue starts a non-peak section [then] drop_swap is refused with bar and label, others still offer, [else stop]."""
    incoming = replace(IN, transition_in_bar=17)
    label = incoming.section_at(17).label
    result = _run(incoming)
    assert not [row for row in result["proposals"] if row["style"] == "drop_swap"]
    reasons = [row["reason"] for row in result["not_offered"] if row["style"] == "drop_swap"]
    assert reasons and all("bar 17" in reason and label in reason for reason in reasons)
    assert result["proposals"], "the proposal did not fall through to another style"


def test_fixture_with_cue_at_bar_13_never_enters_elsewhere() -> None:
    """[if] transition_in_bar=13 [then] no proposed plan of any style enters at a bar other than 13, [else stop]."""
    result = _run(replace(IN, transition_in_bar=13))
    reasons = [row["reason"] for row in result["not_offered"] if row["style"] == "drop_swap"]
    assert reasons and all("bar 13" in reason and "intro" in reason for reason in reasons)
    for intent in ("smooth", "showy", "energy_up"):
        rows = propose_transition(
            OUT, replace(IN, transition_in_bar=13), out_deck=1, in_deck=2, intent=intent, top=None
        )["proposals"]
        assert {row["plan"]["in"]["window"]["start_bar"] for row in rows} <= {13}


def test_no_cue_keeps_drop_swap_on_peak_sections() -> None:
    """[if] the incoming track has no cue [then] drop_swap still enters on peak sections as before, [else stop]."""
    assert drop_swap_cue_refusal(IN) is None
    result = _run(IN)
    swaps = [row for row in result["proposals"] if row["style"] == "drop_swap"]
    assert swaps
    peak_starts = {section.start_bar for section in IN.sections if section.label in PEAK_LABELS}
    assert {row["plan"]["in"]["window"]["start_bar"] for row in swaps} <= peak_starts
