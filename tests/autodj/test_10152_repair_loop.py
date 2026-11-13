"""Invalid proposals are repaired before they are dropped (#10152, spec 05 section 3) on invented tracks."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from apps.autodj import propose
from apps.autodj.profile import TrackProfile
from apps.autodj.propose import propose_transition
from tests.autodj.fixtures import profile

REQ = "TRANSAI-13955"
BLENDS = {"bass_swap_blend", "bass_phrase_swap"}


def _vocal_pair(lo: int, hi: int) -> tuple[TrackProfile, TrackProfile]:
    """Outgoing vocals on bars lo..hi only; incoming vocals on every bar."""
    out, incoming = profile("trk-out"), profile("trk-in", key="9A")
    out = replace(out, bar_vocals=tuple(0.5 if lo <= bar <= hi else 0.0 for bar in range(1, out.bars + 1)))
    return out, replace(incoming, bar_vocals=tuple(0.5 for _ in range(incoming.bars)))


def _run(out: TrackProfile, incoming: TrackProfile, around: int, intent: str = "smooth") -> dict:
    return propose_transition(out, incoming, out_deck=1, in_deck=2, intent=intent, top=None, around_bar=around)


@pytest.mark.requirement(REQ)
def test_repair_rounds_is_three() -> None:
    """[if] the module CFG is read [then] REPAIR_ROUNDS is 3, [else stop]."""
    assert propose.REPAIR_ROUNDS == 3


@pytest.mark.requirement(REQ)
def test_a_vocal_clash_blend_is_repaired_by_moving_the_exit() -> None:
    """[if] a blend from bar 97 clashes on vocals 100-101 [then] it is kept with its exit moved to 113, [else stop]."""
    rows = [row for row in _run(*_vocal_pair(100, 101), 113)["proposals"] if row["style"] in BLENDS]
    assert rows and all(row["validation"]["ok"] for row in rows)
    assert rows[0]["repairs"] == ["exit moved 97 -> 113 (vocal run)"]
    assert rows[0]["rationale"].startswith("repaired:") and rows[0]["plan"]["out"]["window"]["start_bar"] == 113


@pytest.mark.requirement(REQ)
def test_every_pair_overlapping_vocals_yields_a_cut_after_the_vocal_run() -> None:
    """[if] every window pair overlaps vocals [then] a cut after the vocal run returns, blends rejected as vocal, [else stop]."""
    result = _run(*_vocal_pair(81, 124), 105)
    cuts = [row for row in result["proposals"] if row["style"] == "cut" and row.get("repairs")]
    assert cuts and all(row["plan"]["out"]["window"]["start_bar"] > 124 for row in cuts)
    assert all(row["repairs"][0].startswith("switched") for row in cuts)
    assert {row["style"] for row in result["rejected"] if row["gate"] == "vocal"} >= BLENDS


@pytest.mark.requirement(REQ)
def test_an_unrepairable_candidate_is_rejected_with_its_last_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] no ladder step can clear the vocal gate [then] the blend is in rejected with gate vocal, [else stop]."""
    real = propose.place_hit

    def no_cut(style, *args, **kwargs):  # every cut placement refused, so the ladder's last step fails too
        hit = real(style, *args, **kwargs)
        return replace(hit, refused="test: no cut") if style.name == "cut" else hit

    monkeypatch.setattr(propose, "place_hit", no_cut)
    result = _run(*_vocal_pair(81, 124), 105)
    assert not [row for row in result["proposals"] if row["style"] in BLENDS]
    assert {row["style"] for row in result["rejected"] if row["gate"] == "vocal"} >= BLENDS


@pytest.mark.requirement(REQ)
def test_an_all_valid_pair_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] candidates validate first time [then] repairs are empty and the list is byte-identical, [else stop]."""
    out, incoming = _vocal_pair(0, 0)
    with_repair = _run(out, incoming, 113)
    assert all("repairs" not in row for row in with_repair["proposals"])
    monkeypatch.setattr(propose, "REPAIR_ROUNDS", 0)
    without = _run(out, incoming, 113)
    assert json.dumps(with_repair["proposals"], sort_keys=True) == json.dumps(without["proposals"], sort_keys=True)


@pytest.mark.requirement(REQ)
@pytest.mark.parametrize("rounds", [0, 1, 3])
def test_the_ladder_is_bounded_by_repair_rounds(monkeypatch: pytest.MonkeyPatch, rounds: int) -> None:
    """[if] candidates fail [then] repair validations never exceed REPAIR_ROUNDS per failing candidate, [else stop]."""
    calls: list[str] = []
    real = propose.validate_plan

    def counting(plan, *args, **kwargs):
        calls.append(plan.rationale or "")
        return real(plan, *args, **kwargs)

    monkeypatch.setattr(propose, "validate_plan", counting)
    monkeypatch.setattr(propose, "REPAIR_ROUNDS", rounds)
    result = _run(*_vocal_pair(81, 124), 105)
    repaired_calls = sum(1 for text in calls if text.startswith("repaired:"))
    failing = sum(1 for row in result["rejected"] if row["gate"] == "vocal")
    assert repaired_calls <= rounds * failing
    if rounds == 0:
        assert repaired_calls == 0 and not any(row.get("repairs") for row in result["proposals"])
