"""The app's planned transition (AGENT-48, AGENT-52).

``POST /api/v1/dj/transition/prepare`` renders and scores a pair ahead of time, in the background: the page calls it when
a track is loaded on the idle deck, and it answers at once with how far that has got. ``POST /api/v1/dj/transition/plan``
is the press: it never renders, and the page runs the returned order on its own plan runner. ``order`` null is an
answer, not a failure: ``reason`` says why and the page runs its fallback. 409 ``no_performance_page``: no page has
published a mirror. 422 ``deck_empty``: a named deck holds no track.

``POST /api/v1/dj/transition/decide`` (AUTOPLAN-03) is AutoPlay's early decision: the mix-out window ahead in the master and
which shortlisted track to bring in. It renders nothing. ``track`` null is an answer with a ``reason``, as with ``order``.
``GET /api/v1/dj/transition/autoplay-decision`` reads back what the page decided, from its mirror.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.autodj.http import (
    Decision,
    PairTransition,
    TrackProfile,
    build_service,
    decide,
    get_pair_transition,
    heard_profile,
    save_decision,
)
from apps.shared.state import db as state_db_module
from apps.transition_score.live import (
    DeckNotPlannable,
    LiveAnswer,
    LiveRequest,
    PlanCache,
    _bar_now,
    _key,
    _track,
    plan_live,
    prepare_live,
)
from apps.transition_score.tryer import PairNotTryable, TryError

from ..deps import get_library_data_dir, get_library_state_db
from ..errors import ErrorBody
from .dj_try import ServingTrackSource, _serving_origin

router = APIRouter(prefix="/dj/transition", tags=["dj-transition"])
MOUNT_UNDER_API_PREFIX = True
_log = logging.getLogger(__name__)
#: One prepare at a time: it already fans out to the render workers, and a set only needs the current pair.
_PREPARE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transition-prepare")


class TransitionPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    out_deck: int = Field(ge=1, le=4)
    in_deck: int = Field(ge=1, le=4)
    #: Free text or an intent name; empty means the planner's default (smooth).
    intent: str | None = None
    #: Styles already played this set, oldest first: the chooser's variety memory.
    recent_styles: list[str] = Field(default_factory=list, max_length=64)
    #: AGENT-50: leave the outgoing track near this point, in ms on its own clock (a right-click on its waveform).
    from_ms: float | None = Field(default=None, ge=0)
    #: AGENT-50: plan as if the incoming deck were idle; the caller stops and re-cues it before running the order.
    take_incoming: bool = False


class TransitionPrepareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    out_deck: int = Field(ge=1, le=4)
    in_deck: int = Field(ge=1, le=4)
    intent: str | None = None
    #: AUTOPLAN-04: read how far it has got without trying a failed prepare again, so a pair that cannot render says so.
    status_only: bool = False
    #: AUTOPLAN-04: score the window near this point first, ms on the outgoing track's clock: AutoPlay has already chosen it.
    from_ms: float | None = Field(default=None, ge=0)


class TransitionPrepareOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["warming", "ready", "failed"]
    #: Candidates scored so far; a press can use them while the rest of the track warms.
    candidates: int
    reason: str
    render_ms: float
    #: The incoming track this answer is about, as the mirror shows it: a load the page has just made may not be on it yet.
    in_track: str | None


class TransitionDecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    out_deck: int = Field(ge=1, le=4)
    in_deck: int = Field(ge=1, le=4)
    #: AutoPlay's own picks, best first: the chooser picks among these, never beyond them.
    shortlist: list[str] = Field(min_length=1, max_length=8)
    #: AUTOPLAY-HEARD: the key shift each shortlisted track would load with (the page's own load rule); absent means 0.
    in_key_shifts: dict[str, Annotated[int, Field(ge=-6, le=6)]] = Field(default_factory=dict)
    intent: str | None = None
    recent_styles: list[str] = Field(default_factory=list, max_length=64)
    #: The earliest a window may start, ms on the outgoing track's clock: what the page can still reach.
    earliest_ms: float = Field(ge=0)
    #: The soft target, ms on the outgoing track's clock.
    target_ms: float = Field(ge=0)


class TransitionWindowOut(BaseModel):
    start_bar: int
    start_ms: float
    max_bars: int
    label: str
    energy: float | None
    vocal_free_bars: int | None
    score: float
    reason: str


class TransitionDecideOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track: str | None
    window: TransitionWindowOut | None
    style: str | None
    bars: int | None
    reason: str
    #: The ranked windows searched, best first.
    windows: list[TransitionWindowOut]
    #: Shortlisted tracks with no plan, and why.
    rejected: list[dict[str, str]]
    out_bar_now: float
    #: Always false here: nothing has rendered, so the proposal's rule score stood in for M.
    m_measured: Literal[False]


class TransitionPickOut(BaseModel):
    candidate: str
    style: str
    bars: int
    #: The scorer's M, 0..1: an eligibility floor, not the reason this style won. Null when it was not measured yet.
    score: float | None = Field(ge=0.0, le=1.0)
    out_start_bar: int
    rationale: str


class TransitionPlanOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: dict[str, Any] | None
    reason: str
    intent: str
    pick: TransitionPickOut | None
    out_bar_now: float | None
    considered: int
    rendered: int
    propose_ms: float
    render_ms: float
    #: Whether the pick was chosen with M from rendered audio.
    scored: bool
    plan_ms: float
    #: AGENT-49: the pair's saved plan was replayed. Report completion with no plan, so its play count moves.
    saved: bool
    #: The plan document that runs, to send to /autodj/pair-transition/played when it completes.
    plan: dict[str, Any] | None


def _window_document(profile: TrackProfile, window: Any) -> TransitionWindowOut:
    summary = window.summary()
    return TransitionWindowOut(
        start_bar=summary["start_bar"],
        start_ms=round(profile.time_of_bar(window.start_bar) * 1000.0, 1),
        max_bars=summary["max_bars"],
        label=summary["label"],
        energy=summary["energy"],
        vocal_free_bars=summary["vocal_free_bars"],
        score=summary["score"],
        reason=summary["reason"],
    )


def _decide_document(profile: TrackProfile, decision: Decision, bar_now: float) -> TransitionDecideOut:
    return TransitionDecideOut(
        track=decision.track,
        window=None if decision.window is None else _window_document(profile, decision.window),
        style=decision.style,
        bars=decision.bars,
        reason=decision.reason,
        windows=[_window_document(profile, window) for window in decision.windows],
        rejected=[dict(row) for row in decision.rejected],
        out_bar_now=round(bar_now, 2),
        m_measured=False,
    )


def _pick_document(answer: LiveAnswer) -> TransitionPickOut | None:
    if answer.candidate is not None:
        plan, cid, score = answer.candidate.plan, answer.candidate.id, answer.candidate.score
    elif answer.unscored_plan is not None:
        plan, cid, score = answer.unscored_plan, "unscored", None
    else:
        return None
    return TransitionPickOut(
        candidate=cid,
        style=str(plan["style"]),
        bars=int(plan["out"]["window"]["bars"]),
        score=score,
        out_start_bar=plan["out"]["window"]["start_bar"],
        rationale=str(plan.get("rationale") or ""),
    )


def _document(answer: LiveAnswer) -> TransitionPlanOut:
    return TransitionPlanOut(
        order=answer.order,
        reason=answer.reason,
        intent=answer.intent,
        pick=_pick_document(answer),
        out_bar_now=answer.out_bar_now,
        considered=answer.considered,
        rendered=answer.rendered,
        propose_ms=answer.propose_ms,
        render_ms=answer.render_ms,
        scored=answer.scored,
        plan_ms=answer.plan_ms,
        saved=answer.saved,
        plan=answer.unscored_plan if answer.candidate is None else answer.candidate.plan,
    )


def _plan_cache(request: Request) -> PlanCache:
    cache = getattr(request.app.state, "transition_plan_cache", None)
    if cache is None:
        cache = PlanCache()
        request.app.state.transition_plan_cache = cache
    return cache


def _mirror_reader(request: Request) -> Callable[[], Mapping[str, Any]]:
    def read() -> Mapping[str, Any]:
        document = _published_mirror(request)
        if document is None:
            raise LookupError("no page has published a mirror")
        return document

    return read


def _refusal(code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=code, content=ErrorBody(error=error, message=message).model_dump())


def _published_mirror(request: Request) -> Mapping[str, Any] | None:
    mirror = getattr(request.app.state, "ui_mirror", None)
    return mirror if isinstance(mirror, dict) else None


@router.post(
    "/plan",
    response_model=TransitionPlanOut,
    responses={409: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
def plan_transition(
    request: Request,
    body: TransitionPlanIn,
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
) -> TransitionPlanOut | JSONResponse:
    if body.out_deck == body.in_deck:
        return _refusal(422, "same_deck", f"out_deck and in_deck are both {body.out_deck}")
    autodj = build_service()
    source = ServingTrackSource(data_dir, autodj, origin=_serving_origin(request, state_db), state_db=state_db)
    live = LiveRequest(
        body.out_deck, body.in_deck, body.intent, tuple(body.recent_styles), body.from_ms, body.take_incoming
    )

    def saved_for(out_id: str, in_id: str) -> PairTransition | None:
        conn = state_db_module.open_rw(state_db)
        try:
            return get_pair_transition(conn, out_id, in_id)
        finally:
            conn.close()

    try:
        answer = plan_live(
            live,
            autodj=autodj,
            mirror=_mirror_reader(request),
            source=source,
            cache=_plan_cache(request),
            saved_for=saved_for,
        )
        return _document(answer)
    except LookupError as error:
        return _refusal(status.HTTP_409_CONFLICT, "no_performance_page", str(error))
    except DeckNotPlannable as error:
        return _refusal(status.HTTP_422_UNPROCESSABLE_CONTENT, "deck_empty", str(error))


def _deck_state(mirror: Mapping[str, Any], deck: int) -> Mapping[str, Any]:
    decks = mirror.get("decks")
    state = decks.get(str(deck)) if isinstance(decks, Mapping) else None
    return state if isinstance(state, Mapping) else {}


def _record_decision(
    state_db: Path, out_id: str, decision: Decision, body: TransitionDecideIn, unprofiled: list[dict[str, str]]
) -> None:
    """AUTOPLAY-8881: keep the decision beside the pair's plan, for ``GET /dj/transitions/last``. Pure logging; an
    in track of "" stores a decision that chose nothing. The shortlist is the one asked, and tracks dropped before
    planning (no analysis) are kept among the rejections, and the intent and recent styles the chooser weighed (its variety
    memory) are kept too, so a loss to intent or to the variety cap can be explained."""
    stored = decision.to_json()
    document = {
        **stored,
        "shortlist": list(body.shortlist),
        "rejected": [*unprofiled, *stored["rejected"]],
        "intent": body.intent,
        "recent_styles": list(body.recent_styles),
    }
    conn = state_db_module.open_rw(state_db)
    try:
        with state_db_module.write_unit(conn, "pair_decision"):
            save_decision(conn, out_id, decision.track or "", document)
    finally:
        conn.close()


def _shortlist_profiles(
    source: ServingTrackSource, shortlist: list[str]
) -> tuple[list[TrackProfile], list[dict[str, str]]]:
    """The shortlist's profiles, and the tracks that have none with why: one unanalyzed track does not end the decision."""
    profiles: list[TrackProfile] = []
    rejected: list[dict[str, str]] = []
    for stable_id in shortlist:
        try:
            profiles.append(source.profile(stable_id))
        except (PairNotTryable, TryError) as error:
            rejected.append({"track": stable_id, "reason": str(error)})
    return profiles, rejected


@router.post(
    "/decide",
    response_model=TransitionDecideOut,
    responses={409: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
def decide_transition(
    request: Request,
    body: TransitionDecideIn,
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
) -> TransitionDecideOut | JSONResponse:
    """AUTOPLAN-03: the window and track AutoPlay will plan for. Nothing renders, so this answers in the time of a poll."""
    if body.out_deck == body.in_deck:
        return _refusal(422, "same_deck", f"out_deck and in_deck are both {body.out_deck}")
    mirror = _published_mirror(request)
    if mirror is None:
        return _refusal(status.HTTP_409_CONFLICT, "no_performance_page", "no page has published a mirror")
    autodj = build_service()
    source = ServingTrackSource(data_dir, autodj, origin=_serving_origin(request, state_db), state_db=state_db)
    live = LiveRequest(body.out_deck, body.in_deck, body.intent, tuple(body.recent_styles))
    try:
        out_id = _track(autodj, mirror, body.out_deck)
        out_profile = autodj.heard_on_deck(
            source.profile(out_id), _deck_state(mirror, body.out_deck), with_tempo=True
        )
        bar_now = _bar_now(autodj, out_profile, mirror, body.out_deck)
    except DeckNotPlannable as error:
        return _refusal(status.HTTP_422_UNPROCESSABLE_CONTENT, "deck_empty", str(error))
    except (PairNotTryable, TryError) as error:
        return _refusal(status.HTTP_422_UNPROCESSABLE_CONTENT, "no_analysis", str(error))
    profiles, unprofiled = _shortlist_profiles(source, body.shortlist)
    follower_range = _deck_state(mirror, body.in_deck).get("pitch_range")
    reach = float(follower_range) if isinstance(follower_range, (int, float)) and follower_range > 0 else None
    profiles = [
        heard_profile(
            profile, key_shift=body.in_key_shifts.get(profile.stable_id, 0), pitch_range_pct=reach
        )
        for profile in profiles
    ]
    decision = decide(
        out_profile,
        profiles,
        decks=(live.out_deck, live.in_deck),
        intent=live.intent,
        recent_styles=live.recent_styles,
        earliest_bar=max(out_profile.bar_at(body.earliest_ms / 1000.0), bar_now),
        target_bar=out_profile.bar_at(body.target_ms / 1000.0),
        live=mirror,
    )
    answer = _decide_document(out_profile, decision, bar_now)
    answer.rejected = [*unprofiled, *answer.rejected]
    _record_decision(state_db, out_id, decision, body, unprofiled)
    return answer


@router.get("/autoplay-decision", response_model=None, responses={409: {"model": ErrorBody}})
def autoplay_decision(request: Request) -> dict[str, Any] | JSONResponse:
    """AUTOPLAN-06: what AutoPlay has decided, as the page published it. Null decision: the page predates this mirror field."""
    mirror = _published_mirror(request)
    if mirror is None:
        return _refusal(status.HTTP_409_CONFLICT, "no_performance_page", "no page has published a mirror")
    return {"autoplay_decision": mirror.get("autoplay_decision")}


def _log_failure(future: Future[None]) -> None:
    error = future.exception()
    if error is not None:
        _log.error("transition prepare failed", exc_info=error)


@router.post(
    "/prepare",
    response_model=TransitionPrepareOut,
    responses={409: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
def prepare_transition(
    request: Request,
    body: TransitionPrepareIn,
    state_db: Path = Depends(get_library_state_db),  # noqa: B008  # FastAPI DI
    data_dir: Path = Depends(get_library_data_dir),  # noqa: B008  # FastAPI DI
) -> TransitionPrepareOut | JSONResponse:
    """Start scoring the pair if nothing has, and say how far it has got. Safe to call repeatedly."""
    if body.out_deck == body.in_deck:
        return _refusal(422, "same_deck", f"out_deck and in_deck are both {body.out_deck}")
    read = _mirror_reader(request)
    cache = _plan_cache(request)
    live = LiveRequest(body.out_deck, body.in_deck, body.intent, (), body.from_ms)
    try:
        autodj = build_service()
        key = _key(autodj, live, read())
    except LookupError as error:
        return _refusal(status.HTTP_409_CONFLICT, "no_performance_page", str(error))
    except DeckNotPlannable as error:
        return _refusal(status.HTTP_422_UNPROCESSABLE_CONTENT, "deck_empty", str(error))
    if not (body.status_only and cache.get(key) is not None) and cache.begin(key):
        source = ServingTrackSource(data_dir, autodj, origin=_serving_origin(request, state_db), state_db=state_db)
        _PREPARE_POOL.submit(
            prepare_live, live, key, autodj=autodj, mirror=read, source=source, cache=cache
        ).add_done_callback(_log_failure)
    entry = cache.get(key)
    if entry is None:
        return TransitionPrepareOut(state="warming", candidates=0, reason="", render_ms=0.0, in_track=key.in_id)
    return TransitionPrepareOut(
        state=entry.state,  # type: ignore[arg-type]  # PlanCache only writes these three
        candidates=len(entry.candidates),
        reason=entry.reason,
        render_ms=round(entry.render_ms, 1),
        in_track=key.in_id,
    )
