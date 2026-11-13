"""AGENT-48: the app's planner call, over real profiles and the real chooser; only the renderer is a double."""

from __future__ import annotations

import json
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from apps.autodj.contract import ValidationResult
from apps.autodj.http import build_service
from apps.autodj.pair_memory import PairTransition, get_pair_transition, record_played
from apps.autodj.plan import parse_plan
from apps.autodj.profile import trim_db_of_knob, trim_knob
from apps.autodj.propose import propose_transition
from apps.transition_score import live as live_module
from apps.transition_score.contract import SCORER_VERSION, Candidate, Measured
from apps.transition_score.live import PREPARE_N, LiveRequest, PlanCache, PrepareKey, plan_live, prepare_live
from apps.transition_score.tryer import TryError
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from tests.autodj.fixtures import profile
from tests.transition_score.arrangements import IN_ARRANGEMENT

OUT, IN = profile("trk-out"), profile("trk-in", key="9A", arrangement=IN_ARRANGEMENT)
_AUTODJ = build_service()
plan_live = partial(plan_live, autodj=_AUTODJ)
prepare_live = partial(prepare_live, autodj=_AUTODJ)


class _Source:
    def __init__(self) -> None:
        self.tracks = {OUT.stable_id: OUT, IN.stable_id: IN}

    def profile(self, stable_id: str) -> Any:
        return self.tracks[stable_id]

    def audio_path(self, stable_id: str) -> Path:
        return Path(f"/audio/{stable_id}.wav")


def _deck(stable_id: str, position_ms: float, *, playing: bool) -> dict[str, Any]:
    return {
        "stable_id": stable_id,
        "playing": playing,
        "position": {"ms": position_ms},
        "bpm": 128.0,
        "effective_bpm": 128.0,
        "pitch": 1.0,
        "pitch_range": 8.0,
        "quantized_launch_armed": None,
        "sync": {"mode": "off", "enabled": False},
    }


def _mirror(bar: float = 40.0, *, in_playing: bool = False) -> dict[str, Any]:
    channel = {"fader": 1.0, "trim": 0.5, "eq_low": 0.5, "eq_mid": 0.5, "eq_high": 0.5, "filter": 0.5, "assign": "thru"}
    return {
        "master_deck": 1,
        "decks": {
            "1": _deck("trk-out", OUT.time_of_bar(int(bar)) * 1000.0, playing=True),
            "2": _deck("trk-in", 0.0, playing=in_playing),
        },
        "mixer": {"crossfader": 0.5, "channels": {"1": dict(channel), "2": {**channel, "fader": 0.0}}},
    }


def _scored(plan: Any, cid: str, score: float | None, *, vetoed: bool = False) -> Candidate:
    from apps.transition_score.contract import Veto

    return Candidate(
        id=cid,
        plan_json=json.dumps(plan.to_document()),
        plan_sha256=f"sha-{cid}",
        style=plan.style,
        bars=plan.bars,
        metrics_json=json.dumps({}),
        factors_json=json.dumps({}),
        vetoes=(Veto("clip", 1.0, 0.5),) if vetoed else (),
        measured=Measured(k=5, n=7),
        unknown_json=json.dumps({}),
        score=score,
        render_ms=1.0,
        score_ms=1.0,
        scorer_version=SCORER_VERSION,
    )


def _patch_renders(monkeypatch: pytest.MonkeyPatch, score: Any = None) -> list[Any]:
    """Scores every plan 0.4 (or as ``score(plan)`` says), and records what was rendered."""
    rendered: list[Any] = []

    def fake(plans: list[Any], *_args: Any, **_kwargs: Any) -> tuple[Candidate, ...]:
        rendered.extend(plans)
        return tuple(_scored(p, f"c{i + 1}", score(p) if score else 0.4) for i, p in enumerate(plans))

    monkeypatch.setattr(live_module, "try_plans", fake)
    return rendered


def _request(**over: Any) -> LiveRequest:
    return LiveRequest(**{"out_deck": 1, "in_deck": 2, "intent": "smooth", "recent_styles": (), **over})


def _prepared(monkeypatch: pytest.MonkeyPatch, score: Any = None, mirror: Any = _mirror) -> tuple[PlanCache, list[Any]]:
    """A cache after a real prepare, every plan scored 0.4 (or as ``score(plan)`` says)."""
    rendered = _patch_renders(monkeypatch, score)
    cache = PlanCache()
    key = PrepareKey("trk-out", "trk-in", 1, 2, "smooth", in_pitch_range=8.0)
    assert cache.begin(key)
    prepare_live(_request(), key, mirror=mirror, source=_Source(), cache=cache)
    entry = cache.get(key)
    assert entry is not None and entry.state == "ready", entry
    return cache, rendered


def _fail_on_render(monkeypatch: pytest.MonkeyPatch) -> None:
    def render(*_args: Any, **_kwargs: Any) -> tuple[Candidate, ...]:
        raise AssertionError("a press must never render")

    monkeypatch.setattr(live_module, "try_plans", render)


@pytest.mark.requirement("AGENT-48")
def test_a_prepared_pick_is_the_choosers_with_no_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the pair is prepared [then] the press compiles the intent's first style from the cache and renders nothing, [else stop]."""
    cache, rendered = _prepared(monkeypatch)
    _fail_on_render(monkeypatch)
    answer = plan_live(_request(), mirror=_mirror, source=_Source(), cache=cache)
    assert answer.order is not None and answer.candidate is not None and answer.scored
    plan = answer.order["plan"]
    assert plan["style"] == answer.candidate.style == "long_blend"
    assert plan["clock"]["deck"] == 1 and plan["incoming"]["deck"] == 2
    assert answer.rendered == len(rendered) > 1


@pytest.mark.requirement("AGENT-52")
def test_an_unprepared_pair_plans_from_profiles_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] nothing is prepared [then] the press still compiles the intent's first style, marked unscored, with no render, [else stop]."""
    _fail_on_render(monkeypatch)
    answer = plan_live(_request(), mirror=_mirror, source=_Source(), cache=PlanCache())
    assert answer.order is not None and answer.candidate is None and not answer.scored
    assert answer.order["plan"]["style"] == "long_blend"
    assert "M not measured yet" in answer.reason


@pytest.mark.requirement("AGENT-52")
def test_prepare_covers_later_phrases_within_its_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a pair is prepared [then] phrase starts past the planner's best windows are scored too, up to the budget, [else stop]."""
    _cache, rendered = _prepared(monkeypatch)
    starts = {plan.out.start_bar for plan in rendered}
    assert len(starts) > 3 and len(rendered) <= PREPARE_N


@pytest.mark.requirement("AGENT-52")
def test_prepare_publishes_each_window_and_a_second_claim_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a window finishes [then] a press can use it before the rest, and a second prepare of the key does not start, [else stop]."""
    cache = PlanCache()
    key = PrepareKey("trk-out", "trk-in", 1, 2, "smooth", in_pitch_range=8.0)
    assert cache.begin(key) and not cache.begin(key)
    seen: list[int] = []

    def fake(plans: list[Any], *_args: Any, **_kwargs: Any) -> tuple[Candidate, ...]:
        entry = cache.get(key)
        assert entry is not None
        seen.append(len(entry.candidates))
        return tuple(_scored(p, f"c{i + 1}", 0.4) for i, p in enumerate(plans))

    monkeypatch.setattr(live_module, "try_plans", fake)
    prepare_live(_request(), key, mirror=_mirror, source=_Source(), cache=cache)
    assert seen[0] == 0 and seen[-1] > 0, "each window is published before the next renders"
    cache.finish(key, "failed", "control")
    assert cache.begin(key), "a failed prepare may run again"


@pytest.mark.requirement("AGENT-52")
def test_prepare_runs_before_the_outgoing_deck_plays(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the outgoing deck is still paused when the track loads [then] the prepare still scores candidates, [else stop]."""

    def paused() -> dict[str, Any]:
        document = _mirror()
        document["decks"]["1"]["playing"] = False
        return document

    _cache, rendered = _prepared(monkeypatch, mirror=paused)
    assert rendered


@pytest.mark.requirement("AGENT-48")
def test_variety_moves_the_pick_off_a_style_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the first style has played twice recently [then] another eligible style is picked, [else stop]."""
    cache, _rendered = _prepared(monkeypatch)
    first = plan_live(_request(), mirror=_mirror, source=_Source(), cache=cache)
    assert first.candidate is not None
    style = first.candidate.style
    recent = (style, "wave", style, "melt")
    second = plan_live(_request(recent_styles=recent), mirror=_mirror, source=_Source(), cache=cache)
    assert second.candidate is not None and second.candidate.style != style


@pytest.mark.requirement("AGENT-48")
def test_a_higher_m_does_not_beat_the_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a later style has a higher M [then] the intent's first eligible style still wins, [else stop]."""
    cache, _rendered = _prepared(monkeypatch, lambda p: 0.45 if p.style == "long_blend" else 0.5)
    answer = plan_live(_request(), mirror=_mirror, source=_Source(), cache=cache)
    assert answer.candidate is not None and answer.candidate.style == "long_blend"


@pytest.mark.requirement("AGENT-48")
def test_a_window_the_playhead_passed_is_never_picked(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the playhead moved past the prepared window [then] only a plan still ahead of it is compiled, [else stop]."""
    cache, _rendered = _prepared(monkeypatch)
    first = plan_live(_request(), mirror=_mirror, source=_Source(), cache=cache)
    assert first.order is not None
    start = first.order["plan"]["start_bar"]
    later = plan_live(_request(), mirror=lambda: _mirror(start), source=_Source(), cache=cache)
    assert later.order is not None and later.order["plan"]["start_bar"] > start


@pytest.mark.requirement("AGENT-48")
def test_a_pick_that_fails_live_validation_is_dropped_for_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the chooser's first pick does not validate [then] the next is compiled and only picks are validated, [else stop]."""
    cache, _rendered = _prepared(monkeypatch)
    real = _AUTODJ.validate_plan
    calls: list[str] = []

    def refuse_first(plan: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(plan.style)
        result = real(plan, *args, **kwargs)
        if len(calls) == 1:
            return ValidationResult(ok=False, errors=(*result.errors, "control: refused"))
        return result

    monkeypatch.setattr(_AUTODJ, "validate_plan", refuse_first)
    answer = plan_live(_request(), mirror=_mirror, source=_Source(), cache=cache)
    assert answer.order is not None and len(calls) == 2


@pytest.mark.requirement("AGENT-48")
def test_no_window_left_is_a_reason_not_an_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the playhead is past the last out window [then] no order, and the reason says so, [else stop]."""
    _fail_on_render(monkeypatch)
    answer = plan_live(_request(), mirror=lambda: _mirror(OUT.bars - 1), source=_Source(), cache=PlanCache())
    assert answer.order is None and "no valid transition" in answer.reason


@pytest.mark.requirement("AGENT-48")
def test_nothing_eligible_in_the_cache_falls_to_the_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] every scored candidate is vetoed [then] the press plans from profiles, marked unscored, [else stop]."""
    cache = PlanCache()
    key = PrepareKey("trk-out", "trk-in", 1, 2, "smooth", in_pitch_range=8.0)
    cache.begin(key)
    plan = parse_plan(propose_transition(OUT, IN, out_deck=1, in_deck=2, min_out_bar=60, top=1)["proposals"][0]["plan"])
    cache.publish(key, [_scored(plan, "c1", 0.0, vetoed=True)], 1.0)
    answer = plan_live(_request(), mirror=_mirror, source=_Source(), cache=cache)
    assert answer.order is not None and not answer.scored


@pytest.mark.requirement("AGENT-48")
def test_a_render_failure_fails_the_prepare_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the renderer fails [then] the entry says failed with the reason, and a press still plans, [else stop]."""

    def broken(*_args: Any, **_kwargs: Any) -> tuple[Candidate, ...]:
        raise TryError("renderer missing")

    monkeypatch.setattr(live_module, "try_plans", broken)
    cache = PlanCache()
    key = PrepareKey("trk-out", "trk-in", 1, 2, "smooth", in_pitch_range=8.0)
    cache.begin(key)
    prepare_live(_request(), key, mirror=_mirror, source=_Source(), cache=cache)
    entry = cache.get(key)
    assert entry is not None and entry.state == "failed" and entry.reason == "renderer missing"
    assert plan_live(_request(), mirror=_mirror, source=_Source(), cache=cache).order is not None


@pytest.mark.requirement("AGENT-48")
def test_a_playing_incoming_deck_is_refused_unless_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the incoming deck plays [then] no order; when the caller takes it [then] a plan comes back, [else stop]."""
    _fail_on_render(monkeypatch)
    playing = lambda: _mirror(in_playing=True)  # noqa: E731
    refused = plan_live(_request(), mirror=playing, source=_Source(), cache=PlanCache())
    assert refused.order is None and "already playing" in refused.reason
    taken = plan_live(_request(take_incoming=True), mirror=playing, source=_Source(), cache=PlanCache())
    assert taken.order is not None


@pytest.mark.requirement("AGENT-50")
def test_around_here_leaves_from_the_phrase_start_nearest_the_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the user picks a late point [then] the plan leaves from the phrase start nearest it, cached or not, [else stop]."""
    cache, _rendered = _prepared(monkeypatch)
    _fail_on_render(monkeypatch)
    free = plan_live(_request(), mirror=_mirror, source=_Source(), cache=cache)
    assert free.order is not None
    target = next(s.start_bar for s in reversed(OUT.sections) if s.start_bar != free.order["plan"]["start_bar"])
    from_ms = OUT.time_of_bar(target) * 1000.0 + 300.0
    for used in (cache, PlanCache()):
        here = plan_live(_request(from_ms=from_ms), mirror=_mirror, source=_Source(), cache=used)
        assert here.order is not None and here.order["plan"]["start_bar"] == target


@pytest.mark.requirement("AGENT-50")
def test_a_click_inside_the_lead_is_refused_not_moved_later(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the nearest phrase start is inside the lead [then] no order and the reason says so, [else stop]."""
    _fail_on_render(monkeypatch)
    from_ms = OUT.time_of_bar(33) * 1000.0
    near = plan_live(_request(from_ms=from_ms), mirror=lambda: _mirror(32.0), source=_Source(), cache=PlanCache())
    assert near.order is None and "too close to the playhead" in near.reason


@pytest.mark.requirement("AGENT-48")
def test_a_track_changing_before_prepare_starts_fails_that_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a deck loads another track before the prepare runs [then] nothing is scored for the old pair, [else stop]."""
    rendered = _patch_renders(monkeypatch)
    cache = PlanCache()
    key = PrepareKey("trk-out", "trk-in", 1, 2, "smooth", in_pitch_range=8.0)
    cache.begin(key)
    changed = _mirror()
    changed["decks"]["2"]["stable_id"] = "trk-other"
    prepare_live(_request(), key, mirror=lambda: changed, source=_Source(), cache=cache)
    entry = cache.get(key)
    assert entry is not None and entry.state == "failed" and rendered == []


@pytest.mark.requirement("AGENT-52")
def test_a_prepare_with_nothing_to_offer_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the outgoing track is past its last window [then] the entry says failed with the reason, never ready, [else stop]."""
    rendered = _patch_renders(monkeypatch)
    cache = PlanCache()
    key = PrepareKey("trk-out", "trk-in", 1, 2, "smooth", in_pitch_range=8.0)
    cache.begin(key)
    prepare_live(_request(), key, mirror=lambda: _mirror(OUT.bars - 1), source=_Source(), cache=cache)
    entry = cache.get(key)
    assert entry is not None and entry.state == "failed" and "no valid transition" in entry.reason
    assert rendered == []


def _saved(plan_document: dict[str, Any], times: int = 3) -> PairTransition:
    stamp = "2026-10-30T09:00:00+00:00"
    return PairTransition(
        "trk-out",
        "trk-in",
        json.dumps(plan_document, sort_keys=True, separators=(",", ":")),
        "planner",
        plan_document["style"],
        stamp,
        stamp,
        times,
    )


def _saved_document(start: int = 97) -> dict[str, Any]:
    rows = propose_transition(OUT, IN, out_deck=1, in_deck=2, min_out_bar=40, top=None, around_bar=float(start))[
        "proposals"
    ]
    live = _mirror()
    at_start = [
        row["plan"]
        for row in rows
        if row["plan"]["out"]["window"]["start_bar"] == start
        and _AUTODJ.validate_plan(parse_plan(row["plan"]), OUT, IN, live=live).ok
    ]
    assert at_start, f"no live-valid saved plan at bar {start}"
    return min(at_start, key=lambda plan: plan["out"]["window"]["bars"])


@pytest.mark.requirement("AGENT-49")
def test_a_saved_plan_runs_instead_of_the_chooser(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the pair has a saved plan still ahead [then] it runs as saved, with the cache ignored, [else stop]."""
    cache, _rendered = _prepared(monkeypatch)
    document = _saved_document()
    answer = plan_live(
        _request(), mirror=_mirror, source=_Source(), cache=cache, saved_for=lambda _o, _i: _saved(document)
    )
    assert answer.order is not None and answer.saved and answer.candidate is None
    assert answer.order["plan"]["start_bar"] == 97 and answer.order["plan"]["style"] == document["style"]
    assert "saved plan for this pair" in answer.reason
    # Control: with no saved plan the same press is the chooser's.
    chosen = plan_live(_request(), mirror=_mirror, source=_Source(), cache=cache)
    assert not chosen.saved and chosen.candidate is not None


@pytest.mark.requirement("AGENT-49")
def test_a_saved_plan_past_the_playhead_moves_to_the_next_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the outgoing deck is past the saved window [then] the plan runs from the next phrase start, [else stop]."""
    _fail_on_render(monkeypatch)
    answer = plan_live(
        _request(),
        mirror=lambda: _mirror(85),
        source=_Source(),
        cache=PlanCache(),
        saved_for=lambda _o, _i: _saved(_saved_document(81)),
    )
    assert answer.order is not None and answer.saved and answer.order["plan"]["start_bar"] == 97


@pytest.mark.requirement("AGENT-49")
def test_a_saved_plan_that_cannot_run_gives_way_to_the_chooser(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the saved plan fails live validation [then] the chooser decides, never an error, [else stop]."""
    _fail_on_render(monkeypatch)
    real = _AUTODJ.validate_plan
    monkeypatch.setattr(
        _AUTODJ,
        "validate_plan",
        lambda plan, *a, **k: (
            ValidationResult(ok=False, errors=("control",)) if plan.bars == 7 else real(plan, *a, **k)
        ),
    )
    document = _saved_document()
    document["out"]["window"]["bars"] = document["in"]["window"]["bars"] = 7
    broken = {**document, "events": [e for e in document["events"] if e.get("at_bar", 0) <= 7]}
    answer = plan_live(
        _request(), mirror=_mirror, source=_Source(), cache=PlanCache(), saved_for=lambda _o, _i: _saved(broken)
    )
    assert not answer.saved and answer.order is not None


@pytest.mark.requirement("AGENT-49")
def test_a_saved_plan_follows_the_deck_and_the_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the pair is on other decks or the user names a point [then] the saved plan moves there, [else stop]."""
    _fail_on_render(monkeypatch)
    document = _saved_document()
    here = OUT.time_of_bar(81) * 1000.0
    answer = plan_live(
        _request(from_ms=here),
        mirror=_mirror,
        source=_Source(),
        cache=PlanCache(),
        saved_for=lambda _o, _i: _saved(document),
    )
    assert answer.order is not None and answer.saved and answer.order["plan"]["start_bar"] == 81


@pytest.mark.requirement("AGENT-52")
def test_a_changed_transition_in_cue_invalidates_the_prepared_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the incoming cue moves after a prepare [then] the press plans fresh, unscored, [else stop]."""
    cache, _rendered = _prepared(monkeypatch)
    _fail_on_render(monkeypatch)
    source = _Source()
    source.tracks[IN.stable_id] = replace(IN, transition_in_bar=33)
    answer = plan_live(_request(), mirror=_mirror, source=source, cache=cache)
    assert answer.order is not None and not answer.scored


@pytest.mark.requirement("AGENT-52")
def test_a_cached_pick_takes_the_live_outgoing_trim(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the outgoing trim moved after the prepare [then] the compiled loudness match uses the live trim, [else stop]."""
    loud = replace(IN, lufs=-8.0, lufs_reason=None)
    source = _Source()
    source.tracks[IN.stable_id] = loud
    source.tracks[OUT.stable_id] = replace(OUT, lufs=-10.0, lufs_reason=None)
    rendered = _patch_renders(monkeypatch)
    cache = PlanCache()
    key = PrepareKey("trk-out", "trk-in", 1, 2, "smooth", in_pitch_range=8.0)
    cache.begin(key)
    prepare_live(_request(), key, mirror=_mirror, source=source, cache=cache)
    assert rendered

    def trim_of(mirror: Any) -> Any:
        answer = plan_live(_request(), mirror=mirror, source=source, cache=cache)
        assert answer.order is not None
        return [c["value"] for c in answer.order["plan"]["prepare"] if c["type"] == "trim"]

    assert trim_of(_turned_down) != trim_of(_mirror)


@pytest.mark.requirement("AGENT-31")
@pytest.mark.requirement("MEMORY-03")
def test_scored_candidate_plan_matches_live_trim_for_save_on_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the outgoing trim is lower at press than at prepare [then] candidate.plan.in.trim_db matches the compiled trim, [else stop]."""
    cache, source = _loud_prepared(monkeypatch)
    entry = cache.get(PrepareKey("trk-out", "trk-in", 1, 2, "smooth", in_pitch_range=8.0))
    assert entry is not None and entry.candidates
    answer = plan_live(_request(), mirror=_turned_down, source=source, cache=cache)
    assert answer.order is not None and answer.candidate is not None
    prepared = next(c for c in entry.candidates if c.id == answer.candidate.id)
    prepared_in_trim = prepared.plan["in"]["trim_db"]
    trim_cmds = [c for c in answer.order["plan"]["prepare"] if c["type"] == "trim"]
    assert trim_cmds
    played_db = trim_db_of_knob(trim_cmds[0]["value"])
    plan_db = answer.candidate.plan["in"]["trim_db"]
    assert plan_db is not None and played_db == pytest.approx(float(plan_db), abs=0.02)
    assert prepared_in_trim is not None and float(prepared_in_trim) != pytest.approx(float(plan_db), abs=0.02)


@pytest.mark.requirement("AGENT-31")
@pytest.mark.requirement("MEMORY-03")
def test_scored_candidate_plan_unchanged_when_trim_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the mirror trim matches prepare [then] candidate.plan equals the cache candidate's plan document, [else stop]."""
    _patch_renders(monkeypatch)
    cache = PlanCache()
    key = PrepareKey("trk-out", "trk-in", 1, 2, "smooth", in_pitch_range=8.0)
    cache.begin(key)
    prepare_live(_request(), key, mirror=_mirror, source=_Source(), cache=cache)
    entry = cache.get(key)
    assert entry is not None and entry.candidates
    answer = plan_live(_request(), mirror=_mirror, source=_Source(), cache=cache)
    assert answer.candidate is not None
    prepared = next(c for c in entry.candidates if c.id == answer.candidate.id)
    assert answer.candidate.plan == prepared.plan


def _loud_prepared(monkeypatch: pytest.MonkeyPatch) -> tuple[PlanCache, _Source]:
    """A ready cache prepared at trim knob 0.5 over loud profiles, so a later trim change moves the match."""
    source = _Source()
    source.tracks[IN.stable_id] = replace(IN, lufs=-8.0, lufs_reason=None)
    source.tracks[OUT.stable_id] = replace(OUT, lufs=-10.0, lufs_reason=None)
    _patch_renders(monkeypatch)
    cache = PlanCache()
    key = PrepareKey("trk-out", "trk-in", 1, 2, "smooth", in_pitch_range=8.0)
    cache.begin(key)
    prepare_live(_request(), key, mirror=_mirror, source=source, cache=cache)
    return cache, source


def _turned_down() -> dict[str, Any]:
    """The prepare-time mirror with the outgoing trim knob lowered to 0.25 (about -6 dB)."""
    document = _mirror()
    document["mixer"]["channels"]["1"]["trim"] = 0.25
    return document


def _compiled_trim(order: dict[str, Any]) -> float:
    """The incoming trim knob the compiled order sets before the transition runs."""
    return next(c["value"] for c in order["plan"]["prepare"] if c["type"] == "trim")


@pytest.mark.requirement("AGENT-31")
@pytest.mark.requirement("MEMORY-03")
def test_a_rejected_pick_never_lends_its_plan_to_the_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the first pick fails live validation [then] the next one's live-trimmed plan returns, [else stop]."""
    cache, source = _loud_prepared(monkeypatch)
    real = _AUTODJ.validate_plan
    validated: list[Any] = []

    def refuse_first(plan: Any, *args: Any, **kwargs: Any) -> Any:
        validated.append(plan)
        result = real(plan, *args, **kwargs)
        if len(validated) == 1:
            return ValidationResult(ok=False, errors=(*result.errors, "control: refused"))
        return result

    monkeypatch.setattr(_AUTODJ, "validate_plan", refuse_first)
    answer = plan_live(_request(), mirror=_turned_down, source=source, cache=cache)
    assert answer.order is not None and answer.candidate is not None and len(validated) == 2
    rejected, ran = validated[0].to_document(), validated[1].to_document()
    plan = answer.candidate.plan
    assert plan != rejected
    assert plan["in"]["trim_db"] != ran["in"]["trim_db"]
    assert plan == {
        **ran,
        "in": {**ran["in"], "trim_db": plan["in"]["trim_db"], "trim_reason": plan["in"]["trim_reason"]},
    }
    assert trim_knob(plan["in"]["trim_db"]) == pytest.approx(_compiled_trim(answer.order))


@pytest.mark.requirement("AGENT-31")
@pytest.mark.requirement("MEMORY-03")
def test_save_on_run_persists_the_live_trim_that_played(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] the live-trimmed pick is saved after it ran [then] the stored trim is the played one, [else stop]."""
    cache, source = _loud_prepared(monkeypatch)
    entry = cache.get(PrepareKey("trk-out", "trk-in", 1, 2, "smooth", in_pitch_range=8.0))
    assert entry is not None
    answer = plan_live(_request(), mirror=_turned_down, source=source, cache=cache)
    assert answer.order is not None and answer.candidate is not None
    prepared = next(c for c in entry.candidates if c.id == answer.candidate.id)
    conn = state_db.open_rw(tmp_path / "state.db")
    try:
        state_schema.apply_migrations(conn)
        record_played(
            conn, "trk-out", "trk-in", plan=answer.candidate.plan, source="planner", style=answer.candidate.style
        )
        saved = get_pair_transition(conn, "trk-out", "trk-in")
    finally:
        conn.close()
    assert saved is not None
    stored = saved.plan["in"]["trim_db"]
    assert stored != pytest.approx(prepared.plan["in"]["trim_db"], abs=0.02)
    assert trim_knob(stored) == pytest.approx(_compiled_trim(answer.order))


@pytest.mark.requirement("AGENT-31")
def test_live_trim_recomputes_match_with_auto_gain_on_the_mirror() -> None:
    """[if] auto_gain is on the mirror [then] _live_trim uses both decks' offsets, [else stop]."""
    out_p = replace(OUT, lufs=-6.0, lufs_reason=None)
    in_p = replace(IN, lufs=-12.0, lufs_reason=None)
    plan = parse_plan(propose_transition(out_p, in_p, out_deck=1, in_deck=2, intent="smooth")["proposals"][0]["plan"])
    mirror = _mirror()
    mirror["mixer"] = {"channels": {"1": {"trim": 0.5, "fader": 1.0}, "2": {"trim": 0.5, "fader": 0.0}}}
    mirror["auto_gain"] = {
        "1": {"state": "ready", "value": {"stable_id": "trk-out", "effective_offset_db": -6.0}},
        "2": {"state": "ready", "value": {"stable_id": "trk-in", "effective_offset_db": 0.0}},
    }
    trimmed = live_module._live_trim(_AUTODJ, plan, out_p, in_p, mirror, 1, 2)
    assert trimmed.in_.trim_db == pytest.approx(0.0)
    del mirror["auto_gain"]
    trimmed = live_module._live_trim(_AUTODJ, plan, out_p, in_p, mirror, 1, 2)
    assert trimmed.in_.trim_db == pytest.approx(6.0)
    assert "the live mirror carries no auto_gain" in (trimmed.in_.trim_reason or "")


@pytest.mark.requirement("AUTOPLAN-04")
def test_a_prepare_that_names_a_point_scores_that_window_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] AutoPlay has decided a window [then] it is the first scored, so the M floor reaches it inside a short wait, [else stop]."""
    scored: list[int] = []

    def record(plans: list[Any], *_args: Any, **_kwargs: Any) -> list[Candidate]:
        scored.append(plans[0].out.start_bar)
        return []

    monkeypatch.setattr(live_module, "renderable_plans", lambda group, *_args, **_kwargs: (group, []))
    monkeypatch.setattr(live_module, "try_plans", record)
    request = LiveRequest(1, 2, "smooth", (), OUT.time_of_bar(65) * 1000.0)
    key = PrepareKey("trk-out", "trk-in", 1, 2, "smooth", in_pitch_range=8.0)
    cache = PlanCache()
    assert cache.begin(key)
    prepare_live(request, key, mirror=_mirror, source=_Source(), cache=cache)
    assert scored[0] == 65, f"if the decided window is not scored first then broken, got {scored}"


@pytest.mark.requirement("AUTOPLAY-8868d")
@pytest.mark.requirement("AUTOPLAY-8870c")
def test_a_reshifted_or_repitched_master_misses_the_prepared_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the master's key shift or pitch changes after a prepare [then] the press misses the cache, [else stop]."""
    cache, _rendered = _prepared(monkeypatch)
    _fail_on_render(monkeypatch)
    assert plan_live(_request(), mirror=_mirror, source=_Source(), cache=cache).scored, "control: same heard state hits"

    def reshifted() -> dict[str, Any]:
        mirror = _mirror()
        mirror["decks"]["1"]["key_sync"] = {"shift_semitones": 1}
        return mirror

    def repitched() -> dict[str, Any]:
        mirror = _mirror()
        mirror["decks"]["1"]["pitch"] = 1.04
        return mirror

    def reranged() -> dict[str, Any]:
        mirror = _mirror()
        mirror["decks"]["2"]["pitch_range"] = 16.0
        return mirror

    for changed in (reshifted, repitched, reranged):
        answer = plan_live(_request(), mirror=changed, source=_Source(), cache=cache)
        assert not answer.scored, f"if a {changed.__name__} master is served the old score then broken"
