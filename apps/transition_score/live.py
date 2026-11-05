"""The app's planner (AGENT-48, AGENT-52): the chooser's pick for the decks as they stand now, without rendering live.

The chooser needs M and M needs rendered audio, which takes seconds. So rendering happens ahead of time:
:func:`prepare_live` runs when a track is loaded on the idle deck and fills a :class:`PlanCache` keyed by the pair, decks
and intent. :func:`plan_live`, on the press, reads the cache: it picks with ``choose_for_set`` over what is scored,
validates the pick on the live mirror and compiles the page order. With nothing scored for that point it plans from the
profiles alone (the intent's first valid style, M unmeasured) and says so, rather than make the operator wait.

No pick is an answer, not an error: ``LiveAnswer.order`` is None and ``reason`` says why, so the page can run its
fallback and say which path ran. A malformed request or mirror raises.

Requirements:
  ✔︎ a press never renders: a pick comes from the cache, or from the profiles with M reported unmeasured
  ✔︎ a pick is valid on the mirror read when it is compiled, or it is dropped and the next one is picked
  ✔︎ prepare publishes each window as it is scored, so a press mid-prepare uses what is ready
Acceptance:
  [if] the pair is prepared [then] the pick is the chooser's over scored candidates, with no render
  [if] the pair is not prepared [then] an unscored pick compiles, marked unscored, with no render
  [if] the first pick no longer validates live [then] the next eligible candidate is compiled instead
  [if] the outgoing track has no out window left after the lead [then ⛔️] no order, and the reason says so
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from apps.autodj.choose import STYLE_CAP, VARIETY_WINDOW, choose_for_set
from apps.autodj.choose import Candidate as ChooserCandidate
from apps.autodj.compile import compile_plan
from apps.autodj.live import ANCHOR_LEAD_BARS, heard_on_deck, lead_short, live_bar, with_in_track
from apps.autodj.pair_memory import PairTransition, on_decks
from apps.autodj.plan import PlanError, TransitionPlan, live_mapping, parse_plan, validate_plan
from apps.autodj.profile import TrackProfile
from apps.autodj.propose import match_live_trim_db, parse_intent, propose_transition
from apps.transition_score.contract import Candidate
from apps.transition_score.tryer import PairNotTryable, TrackSource, TryError, renderable_plans, try_plans

_log = logging.getLogger(__name__)

#: Candidates rendered per live request. The bench renders 24; see the measured cost in (private path).
LIVE_TRY_N = 12
#: Per style, so the render budget reaches the styles the intent ranks: the proposal score alone fills it with one style.
LIVE_PER_STYLE = 2
#: As the bench: context bars either side of the overlap in each render.
LIVE_CONTEXT_BARS = 4
#: Extra bars past the anchor lead a press requires: a press no longer renders, so this only covers cueing the deck.
PLANNING_LEAD_BARS = 1.0
#: Candidates one prepare renders in all, across the pair's windows. At 12 workers this is about 20 s on Silver.
PREPARE_N = 30
#: Candidates per phrase start past the planner's own best windows, so "transition out around here" often hits the cache.
PREPARE_PER_WINDOW = 2
#: Windows rendered at once. Each renders on its own clock, so they cannot share one job; a small window alone would
#: leave the render workers idle while the next waits.
PREPARE_WINDOWS_AT_ONCE = 4
#: Prepared pairs kept: a set only ever looks at the current pair, so this just survives switching decks and back.
CACHE_PAIRS = 8


class DeckNotPlannable(ValueError):
    """A named deck holds no track or reports no playhead: the request's fault, unlike a corrupt mirror."""


@dataclass(frozen=True)
class LiveRequest:
    out_deck: int
    in_deck: int
    intent: str | None
    recent_styles: tuple[str, ...]
    #: AGENT-50: leave the outgoing track near this point (ms on its own clock) rather than at its best window.
    from_ms: float | None = None
    #: AGENT-50: the incoming deck may be playing; the page stops and re-cues it, so plan it as idle.
    take_incoming: bool = False


@dataclass(frozen=True)
class LiveAnswer:
    """The page's ``plan`` order and how it was chosen, or no order and why."""

    order: dict[str, Any] | None
    reason: str
    intent: str
    candidate: Candidate | None = None
    out_bar_now: float | None = None
    considered: int = 0
    rendered: int = 0
    propose_ms: float = 0.0
    render_ms: float = 0.0
    #: Whether the pick was chosen with M from rendered audio, or from the profiles alone.
    scored: bool = False
    #: How long the press itself took on the server, ms.
    plan_ms: float = 0.0
    #: The plan document of a pick with no candidate (unscored, or the pair's saved plan).
    unscored_plan: dict[str, Any] | None = None
    #: AGENT-49: this is the pair's saved plan, replayed rather than chosen.
    saved: bool = False


@dataclass(frozen=True)
class PrepareKey:
    out_id: str
    in_id: str
    out_deck: int
    in_deck: int
    intent: str
    #: AUTOPLAY-8868d: how the decks are heard (applied key shifts, the master's pitch). A score made at one heard key or
    #: tempo is another pair's score once the master is reshifted or repitched mid-play, so it is part of the key.
    out_key_shift: int = 0
    out_pitch: float = 1.0
    in_key_shift: int = 0
    #: AUTOPLAY-8870c: the follower deck's pitch range sets the Beat Sync fold, so plans prepared at one range are another
    #: pair's plans after a range change. 0.0 when the deck reports none (the planner's 6% default).
    in_pitch_range: float = 0.0


@dataclass
class Prepared:
    """One pair's scored candidates so far. ``state`` is warming, ready or failed."""

    state: str = "warming"
    candidates: list[Candidate] = field(default_factory=list)
    reason: str = ""
    render_ms: float = 0.0
    #: The incoming track's transition-in cue when this was scored: a moved cue changes every in-window.
    transition_in_bar: int | None = None


class PlanCache:
    """Prepared pairs, newest last. One prepare at a time per key; renders run outside the lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[PrepareKey, Prepared] = OrderedDict()

    def get(self, key: PrepareKey) -> Prepared | None:
        with self._lock:
            return self._entries.get(key)

    def begin(self, key: PrepareKey) -> bool:
        """Claim ``key`` for a prepare; False when it is warming or ready already. A failed one is tried again."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.state != "failed":
                return False
            self._entries[key] = Prepared()
            self._entries.move_to_end(key)
            while len(self._entries) > CACHE_PAIRS:
                self._entries.popitem(last=False)
            return True

    def note_cue(self, key: PrepareKey, transition_in_bar: int | None) -> None:
        with self._lock:
            self._entries[key].transition_in_bar = transition_in_bar

    def publish(self, key: PrepareKey, candidates: Sequence[Candidate], render_ms: float) -> None:
        with self._lock:
            entry = self._entries[key]
            entry.candidates = [*entry.candidates, *candidates]
            entry.render_ms += render_ms

    def finish(self, key: PrepareKey, state: str, reason: str = "") -> None:
        with self._lock:
            entry = self._entries[key]
            entry.state, entry.reason = state, reason


def _deck(mirror: Mapping[str, Any], deck: int) -> Mapping[str, Any]:
    return live_mapping(
        live_mapping(mirror.get("decks"), "live mirror decks").get(str(deck)), f"live mirror deck {deck}"
    )


def _as_planned(mirror: Mapping[str, Any], request: LiveRequest) -> Mapping[str, Any]:
    """The mirror the plan will run against: a taken incoming deck is stopped by the page before the plan starts."""
    if not request.take_incoming:
        return mirror
    decks = {key: dict(value) if isinstance(value, Mapping) else value for key, value in mirror["decks"].items()}
    decks[str(request.in_deck)]["playing"] = False
    return {**mirror, "decks": decks}


def _bar_now(profile: TrackProfile, mirror: Mapping[str, Any], deck: int) -> float:
    bar = live_bar(profile, _deck(mirror, deck))
    if bar is None:
        raise DeckNotPlannable(f"deck {deck} reports no playhead")
    return bar


def _track(mirror: Mapping[str, Any], deck: int) -> str:
    stable_id = _deck(mirror, deck).get("stable_id")
    if not isinstance(stable_id, str) or not stable_id:
        raise DeckNotPlannable(f"deck {deck} holds no track")
    return stable_id


def _spread(ranked: list[TransitionPlan]) -> list[TransitionPlan]:
    """The best LIVE_PER_STYLE plans of each style, at most LIVE_TRY_N, in proposal rank order."""
    seen: Counter[str] = Counter()
    kept: list[TransitionPlan] = []
    for plan in ranked:
        if seen[plan.style] < LIVE_PER_STYLE:
            seen[plan.style] += 1
            kept.append(plan)
    return kept[:LIVE_TRY_N]


def _nearest_phrase(profile: TrackProfile, around: float) -> int:
    return min((section.start_bar for section in profile.sections), key=lambda bar: (abs(bar - around), bar))


def _inside_lead(profile: TrackProfile, around: float, min_out_bar: float) -> str | None:
    """AGENT-50: why the phrase start nearest the chosen point cannot be used, or None. It is reported, never moved to a
    later phrase: the user named this point, and leaving well after it is a different transition."""
    if not profile.sections:
        return "the outgoing track has no phrases to leave from"
    nearest = _nearest_phrase(profile, around)
    if nearest > min_out_bar:
        return None
    return f"the nearest phrase start, bar {nearest}, is too close to the playhead to plan (it must be past bar {min_out_bar:.0f})"


def _nearest(plans: list[TransitionPlan], around: float | None) -> list[TransitionPlan]:
    """AGENT-50: only the plans leaving from the phrase start nearest the chosen point; the next start is tried only when
    the nearest yields no valid plan at all (it is not in ``plans`` then)."""
    if around is None or not plans:
        return plans
    nearest = min({p.out.start_bar for p in plans}, key=lambda bar: (abs(bar - around), bar))
    return [p for p in plans if p.out.start_bar == nearest]


def _reason(style: str, bars: int, score: float | None, intent: str, recent: Sequence[str]) -> str:
    measured = "M not measured yet" if score is None else f"M {score:.2f}"
    return f"intent {intent}: {style} over {bars} bars, {measured}, {len(recent[-VARIETY_WINDOW:])} recent styles, cap {STYLE_CAP}"


def _heard_pair(source: TrackSource, key: PrepareKey, read: Mapping[str, Any]) -> tuple[TrackProfile, TrackProfile]:
    """AUTOPLAY-HEARD: the pair as the decks play it: the master at its applied key shift and pitch, the incoming
    track at the key shift it was loaded with. A pair is never judged on a key its deck is not playing."""
    return (
        heard_on_deck(source.profile(key.out_id), _deck(read, key.out_deck), with_tempo=True),
        heard_on_deck(source.profile(key.in_id), _deck(read, key.in_deck), with_tempo=False),
    )


def _key(request: LiveRequest, mirror: Mapping[str, Any]) -> PrepareKey:
    return PrepareKey(
        _track(mirror, request.out_deck),
        _track(mirror, request.in_deck),
        request.out_deck,
        request.in_deck,
        parse_intent(request.intent),
        _deck_key_shift(mirror, request.out_deck),
        _deck_pitch(mirror, request.out_deck),
        _deck_key_shift(mirror, request.in_deck),
        _deck_pitch_range(mirror, request.in_deck),
    )


def _deck_key_shift(mirror: Mapping[str, Any], deck: int) -> int:
    key_sync = _deck(mirror, deck).get("key_sync")
    shift = key_sync.get("shift_semitones") if isinstance(key_sync, Mapping) else None
    return shift if isinstance(shift, int) and not isinstance(shift, bool) else 0


def _deck_pitch_range(mirror: Mapping[str, Any], deck: int) -> float:
    pitch_range = _deck(mirror, deck).get("pitch_range")
    return float(pitch_range) if isinstance(pitch_range, (int, float)) and pitch_range > 0 else 0.0


def _deck_pitch(mirror: Mapping[str, Any], deck: int) -> float:
    pitch = _deck(mirror, deck).get("pitch")
    return float(pitch) if isinstance(pitch, (int, float)) and pitch > 0 else 1.0


def _playing_out(mirror: Mapping[str, Any], out_deck: int) -> Mapping[str, Any]:
    """The mirror with the outgoing deck playing: a prepare may run before it starts, and a press validates live."""
    decks = {key: dict(value) if isinstance(value, Mapping) else value for key, value in mirror["decks"].items()}
    decks[str(out_deck)]["playing"] = True
    return {**mirror, "decks": decks}


@dataclass(frozen=True)
class _Proposer:
    """One prepare's proposal call: the pair, its decks and the mirror it plans against."""

    out_profile: TrackProfile
    in_profile: TrackProfile
    request: LiveRequest
    intent: str
    min_out_bar: float
    live: dict[str, Any]

    def plans(self, around: float | None) -> list[TransitionPlan]:
        rows = propose_transition(
            self.out_profile,
            self.in_profile,
            out_deck=self.request.out_deck,
            in_deck=self.request.in_deck,
            intent=self.intent,
            min_out_bar=self.min_out_bar,
            live=self.live,
            top=None,
            around_bar=around,
        )["proposals"]
        return [parse_plan(row["plan"]) for row in rows]


def _by_window(plans: list[TransitionPlan]) -> list[list[TransitionPlan]]:
    groups: dict[int, list[TransitionPlan]] = {}
    for plan in plans:
        groups.setdefault(plan.out.start_bar, []).append(plan)
    return list(groups.values())


def _later_windows(proposer: _Proposer, done: set[int], budget: int) -> list[list[TransitionPlan]]:
    """Every other phrase start past the lead, nearest first, so "transition out around here" often hits the cache."""
    groups: list[list[TransitionPlan]] = []
    for section in proposer.out_profile.sections:
        if budget <= 0:
            break
        if section.start_bar <= proposer.min_out_bar or section.start_bar in done:
            continue
        near = [p for p in _spread(proposer.plans(float(section.start_bar))) if p.out.start_bar == section.start_bar]
        chosen = near[: min(PREPARE_PER_WINDOW, budget)]
        if chosen:
            groups.append(chosen)
            budget -= len(chosen)
    return groups


def prepare_live(
    request: LiveRequest,
    key: PrepareKey,
    *,
    mirror: Callable[[], Mapping[str, Any]],
    source: TrackSource,
    cache: PlanCache,
    binary: Path | None = None,
) -> None:
    """Render and score candidates for ``request``'s pair into ``cache``, one window at a time. Runs off the request."""
    try:
        read = _playing_out(_as_planned(mirror(), request), request.out_deck)
        if _key(request, read) != key:
            cache.finish(key, "failed", "a deck changed track before the prepare started")
            return
        out_profile, in_profile = _heard_pair(source, key, read)
        bar_now = _bar_now(out_profile, read, request.out_deck)
        live = with_in_track(read, request.in_deck, key.in_id, in_profile.bpm)
        min_out_bar = bar_now + ANCHOR_LEAD_BARS + PLANNING_LEAD_BARS
        proposer = _Proposer(out_profile, in_profile, request, key.intent, min_out_bar, live)
        cache.note_cue(key, in_profile.transition_in_bar)

        def score(group: list[TransitionPlan]) -> tuple[list[Candidate], float]:
            renderable, _refused = renderable_plans(group, out_profile, in_profile, LIVE_CONTEXT_BARS)
            started = time.perf_counter()
            scored = try_plans(
                renderable,
                out_profile,
                in_profile,
                (source.audio_path(key.out_id), source.audio_path(key.in_id)),
                context_bars=LIVE_CONTEXT_BARS,
                binary=binary,
            )
            ids = [replace(c, id=f"w{renderable[0].out.start_bar}-{c.id}") for c in scored]
            return ids, (time.perf_counter() - started) * 1000.0

        def score_all(groups: list[list[TransitionPlan]]) -> None:
            with ThreadPoolExecutor(max_workers=PREPARE_WINDOWS_AT_ONCE) as pool:
                for finished in as_completed([pool.submit(score, group) for group in groups]):
                    cache.publish(key, *finished.result())

        # The planner's own windows first: they answer the > button, so a press is warm before the rest is proposed. A
        # request that names a point (AUTOPLAN-04, AutoPlay has already decided) wants that window scored first instead.
        around = None if request.from_ms is None else out_profile.bar_at(request.from_ms / 1000.0)
        best = _by_window(_spread(_nearest(proposer.plans(around), around)))
        score_all(best)
        done = {group[0].out.start_bar for group in best}
        score_all(_later_windows(proposer, done, PREPARE_N - sum(len(group) for group in best)))
    except (PairNotTryable, TryError) as error:
        _log.warning("live prepare: %s -> %s: %s", key.out_id, key.in_id, error)
        cache.finish(key, "failed", str(error))
        return
    except BaseException as error:
        cache.finish(key, "failed", str(error))
        raise
    entry = cache.get(key)
    if entry is None or not entry.candidates:
        # Ready means a press can use it: a track past its last window has nothing to offer, and says so.
        cache.finish(key, "failed", f"no valid transition left after bar {min_out_bar:.0f}")
        return
    cache.finish(key, "ready")


def _pick(
    pool: list[ChooserCandidate],
    plans: Mapping[str, TransitionPlan],
    request: LiveRequest,
    validate: Callable[[TransitionPlan], str | None],
) -> tuple[ChooserCandidate | None, list[str]]:
    """The chooser's pick that still validates, and why the ones before it did not. Only picks are validated."""
    dropped: list[str] = []
    remaining = list(pool)
    while (pick := choose_for_set(remaining, request.intent, request.recent_styles)) is not None:
        why = validate(plans[pick.cid])
        if why is None:
            return pick, dropped
        dropped.append(f"{pick.style}: {why}")
        remaining = [c for c in remaining if c.cid != pick.cid]
    return None, dropped


def _live_trim(
    plan: TransitionPlan,
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    mirror: Mapping[str, Any],
    out_deck: int,
    in_deck: int,
) -> TransitionPlan:
    """A scored plan with its loudness match redone on the live outgoing trim: it was matched when the prepare ran."""
    trim_db, trim_reason = match_live_trim_db(out_profile, in_profile, mirror, out_deck, in_deck)
    document = plan.to_json()
    document["in"]["trim_db"] = trim_db
    document["in"]["trim_reason"] = trim_reason
    return parse_plan(document)


def _reanchored(
    document: Mapping[str, Any], profile: TrackProfile, min_out_bar: float, around: float | None
) -> dict[str, Any] | None:
    """AGENT-49: a saved plan on the live position: its own window while that is ahead, the phrase the user pointed at, or
    the next phrase start. None when no phrase is left. Only the outgoing window moves; the plan is otherwise as saved."""
    start = document["out"]["window"]["start_bar"]
    if around is not None:
        start = _nearest_phrase(profile, around)
    elif start <= min_out_bar:
        later = [section.start_bar for section in profile.sections if section.start_bar > min_out_bar]
        if not later:
            return None
        start = later[0]
    moved = deepcopy(dict(document))
    moved["out"]["window"]["start_bar"] = start
    return moved


def _replay_saved(
    saved: PairTransition,
    request: LiveRequest,
    profiles: tuple[TrackProfile, TrackProfile],
    bounds: tuple[float, float | None],
    validate: Callable[[TransitionPlan], str | None],
) -> tuple[TransitionPlan, str] | None:
    """The saved plan on these decks and this playhead, or None (logged) when it cannot run: the chooser then decides."""
    out_profile, _in_profile = profiles
    min_out_bar, around = bounds
    try:
        document = _reanchored(
            on_decks(saved.plan, request.out_deck, request.in_deck), out_profile, min_out_bar, around
        )
        if document is None:
            return None
        plan = parse_plan(document)
    except PlanError as error:
        _log.warning("saved plan for %s -> %s no longer parses: %s", saved.out_track_id, saved.in_track_id, error)
        return None
    why = validate(plan)
    if why is not None:
        _log.info("saved plan for %s -> %s skipped: %s", saved.out_track_id, saved.in_track_id, why)
        return None
    reason = f"saved plan for this pair ({saved.source}): {plan.style} over {plan.bars} bars, played {saved.times_played} times"
    return plan, reason


def plan_live(
    request: LiveRequest,
    *,
    mirror: Callable[[], Mapping[str, Any]],
    source: TrackSource,
    cache: PlanCache,
    saved_for: Callable[[str, str], PairTransition | None] = lambda _out, _in: None,
) -> LiveAnswer:
    """The pair's saved plan if it can run, else the chooser's pick, as a page order from the playhead ``mirror`` reports.
    Never renders."""
    started = time.perf_counter()
    intent = parse_intent(request.intent)
    read = _as_planned(mirror(), request)
    if _deck(read, request.in_deck).get("playing") is True:
        return LiveAnswer(
            None, f"deck {request.in_deck} is already playing, so it cannot start a planned transition", intent
        )
    key = _key(request, read)
    try:
        out_profile, in_profile = _heard_pair(source, key, read)
    except (PairNotTryable, TryError) as error:
        _log.warning("live plan: no pick for %s -> %s: %s", key.out_id, key.in_id, error)
        return LiveAnswer(None, str(error), intent)
    bar_now = _bar_now(out_profile, read, request.out_deck)
    around = None if request.from_ms is None else out_profile.bar_at(request.from_ms / 1000.0)
    min_out_bar = bar_now + ANCHOR_LEAD_BARS + PLANNING_LEAD_BARS
    base = LiveAnswer(None, "", intent, out_bar_now=round(bar_now, 2))
    too_close = None if around is None else _inside_lead(out_profile, around, min_out_bar)
    if too_close is not None:
        return replace(base, reason=too_close)
    validate = _plan_validator(out_profile, in_profile, read, bar_now)
    saved = saved_for(key.out_id, key.in_id)
    replay = (
        None
        if saved is None
        else _replay_saved(saved, request, (out_profile, in_profile), (min_out_bar, around), validate)
    )
    if replay is not None:
        plan, reason = replay
        order = compile_plan(plan, in_profile, plan_id=f"plan-{uuid.uuid4().hex[:12]}", live=read)
        ms = round((time.perf_counter() - started) * 1000.0, 1)
        return replace(base, order=order, reason=reason, unscored_plan=plan.to_json(), saved=True, plan_ms=ms)

    # A scored window further off than the phrase the user pointed at is not this transition: that phrase plans fresh.
    wanted = None if around is None else _nearest_phrase(out_profile, around)
    scored, by_id, plans = _scored_in_reach(cache.get(key), in_profile, min_out_bar, wanted)
    pool = [ChooserCandidate(c.id, c.style, float(c.score or 0.0), bool(c.vetoes)) for c in by_id.values()]
    pick, dropped = _pick(pool, plans, request, validate)
    if pick is not None:
        chosen = by_id[pick.cid]
        plan = _live_trim(plans[pick.cid], out_profile, in_profile, read, request.out_deck, request.in_deck)
        order = compile_plan(plan, in_profile, plan_id=f"plan-{uuid.uuid4().hex[:12]}", live=read)
        reason = _reason(plan.style, plan.bars, chosen.score, intent, request.recent_styles)
        ms = round((time.perf_counter() - started) * 1000.0, 1)
        return replace(
            base, order=order, reason=reason, candidate=chosen, rendered=len(scored), scored=True, plan_ms=ms
        )
    return _plan_unscored(base, request, read, (out_profile, in_profile), (min_out_bar, around), started, dropped)


def _plan_validator(
    out_profile: TrackProfile, in_profile: TrackProfile, read: Mapping[str, Any], bar_now: float
) -> Callable[[TransitionPlan], str | None]:
    def validate(plan: TransitionPlan) -> str | None:
        validation = validate_plan(plan, out_profile, in_profile, live=read)
        short = lead_short(plan, bar_now)
        return None if validation.ok and short is None else "; ".join([*validation.errors, *([short] if short else [])])

    return validate


def _scored_in_reach(
    entry: Prepared | None, in_profile: TrackProfile, min_out_bar: float, wanted: int | None
) -> tuple[list[Candidate], dict[str, Candidate], dict[str, TransitionPlan]]:
    """The prepared scored candidates, those still ahead of the playhead by id, and their parsed plans."""
    scored = _still_scored(entry, in_profile)
    parsed = {c.id: parse_plan(c.plan) for c in scored}
    plans = {
        cid: plan
        for cid, plan in parsed.items()
        if plan.out.start_bar > min_out_bar and (wanted is None or plan.out.start_bar == wanted)
    }
    return scored, {c.id: c for c in scored if c.id in plans}, plans


def _still_scored(entry: Prepared | None, in_profile: TrackProfile) -> list[Candidate]:
    """The prepared candidates that carry a score, unless the incoming cue moved since they were scored."""
    if entry is None or entry.transition_in_bar != in_profile.transition_in_bar:
        return []
    return [c for c in entry.candidates if c.score is not None]


def _plan_unscored(
    base: LiveAnswer,
    request: LiveRequest,
    read: Mapping[str, Any],
    profiles: tuple[TrackProfile, TrackProfile],
    bounds: tuple[float, float | None],
    started: float,
    dropped: list[str],
) -> LiveAnswer:
    """Nothing scored fits: the intent's first valid style from the profiles, with M reported as not measured."""
    out_profile, in_profile = profiles
    min_out_bar, around = bounds
    proposed = propose_transition(
        out_profile,
        in_profile,
        out_deck=request.out_deck,
        in_deck=request.in_deck,
        intent=request.intent,
        min_out_bar=min_out_bar,
        live=with_in_track(read, request.in_deck, in_profile.stable_id, in_profile.bpm),
        top=None,
        around_bar=around,
    )
    plans = _nearest([parse_plan(row["plan"]) for row in proposed["proposals"]], around)
    considered = int(proposed["considered"])
    ms = round((time.perf_counter() - started) * 1000.0, 1)
    if not plans:
        detail = "; ".join(dropped)
        reason = f"no valid transition after bar {min_out_bar:.0f} ({considered} considered)" + (
            f": {detail}" if detail else ""
        )
        return replace(base, reason=reason, considered=considered, plan_ms=ms)
    by_id = {f"p{index}": plan for index, plan in enumerate(plans)}
    pool = [ChooserCandidate(cid, plan.style, 1.0, False) for cid, plan in by_id.items()]
    pick = choose_for_set(pool, request.intent, request.recent_styles)
    assert pick is not None  # every candidate shares one M and none is vetoed, so the pool cannot empty
    plan = by_id[pick.cid]
    order = compile_plan(plan, in_profile, plan_id=f"plan-{uuid.uuid4().hex[:12]}", live=read)
    reason = _reason(plan.style, plan.bars, None, base.intent, request.recent_styles)
    return replace(
        base,
        order=order,
        reason=reason,
        considered=considered,
        unscored_plan=plan.to_json(),
        plan_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
