"""Intent dispatch.

Each ``Intent`` maps to a handler that: (a) consults context (mute /
debounce / destructive mode), (b) publishes an event on the bus, and
(c) drives the TTS engine. Destructive handlers (``SAVE_CUE``,
``RATE_TRACK``) publish ``dry_run: true`` events unless
``context.destructive`` is True **and** the confirmation loop
returns True. Plan 3 wires the confirmation loop; Plan 2 always takes
the ``dry_run: true`` branch.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from apps.voice.context import VoiceContext
from apps.voice.grammar import Intent


@dataclass
class Response:
    """Dispatch outcome."""

    reply: str
    published: bool
    event_id: int | None = None
    dry_run: bool = False
    meta: dict[str, Any] | None = None


Handler = Callable[[Intent, VoiceContext], Response]
DEBOUNCE_EXEMPT_INTENTS: frozenset[str] = frozenset({"MUTE_VOICE", "UNMUTE_VOICE"})


def _publish(
    ctx: VoiceContext,
    kind: str,
    slots: dict[str, Any] | None = None,
    dry_run: bool = False,
    **extra: Any,
) -> int:
    event = {
        "kind": kind,
        "slots": dict(slots or {}),
        "dry_run": dry_run,
        **extra,
    }
    return int(ctx.event_bus.publish(event))


def _speak(ctx: VoiceContext, text: str) -> None:
    ctx.tts_engine.speak(text)


# ----- Read-only handlers ---------------------------------------------------


def handle_search(intent: Intent, ctx: VoiceContext) -> Response:
    query = str(intent.slots.get("query", "")).strip()
    if not query:
        _speak(ctx, "didn't catch the search query")
        return Response(reply="search_empty", published=False)
    # Pending apps.shared.state.tracks module (search index shipped under
    # Phase 6 analysis but the public ``tracks.search`` helper is not yet
    # exposed from apps/shared/state/). This handler soft-imports it so the
    # wiring activates automatically once the module lands; until then the
    # event is published with an empty results list.
    try:
        from apps.shared.state import tracks as _tracks  # type: ignore[attr-defined]

        results = list(_tracks.search(query, limit=5))  # type: ignore[attr-defined]
    except Exception:
        results = []
    eid = _publish(
        ctx,
        "SEARCH",
        slots={"query": query},
        transcript=intent.raw_transcript,
        confidence=intent.confidence,
        results=[
            {"stable_id": r.get("stable_id"), "title": r.get("title")} for r in results
        ],
    )
    _speak(ctx, f"searching for {query}")
    ctx.mark_dispatch()
    return Response(
        reply=f"search:{query}",
        published=True,
        event_id=eid,
        meta={"results": results},
    )


def handle_read_bpm(intent: Intent, ctx: VoiceContext) -> Response:
    deck_state = _latest_deck_state(ctx)
    if not deck_state:
        _speak(ctx, "no deck playing")
        return Response(reply="no_deck_state", published=False)
    bpm = deck_state.get("bpm")
    if bpm is None:
        _speak(ctx, "bpm unknown")
        return Response(reply="bpm_unknown", published=False)
    rounded = round(float(bpm))
    eid = _publish(
        ctx,
        "READ_BPM",
        slots={"bpm": rounded},
        transcript=intent.raw_transcript,
        confidence=intent.confidence,
    )
    _speak(ctx, f"{rounded} BPM")
    ctx.mark_dispatch()
    return Response(reply=f"bpm:{rounded}", published=True, event_id=eid)


def handle_read_key(intent: Intent, ctx: VoiceContext) -> Response:
    deck_state = _latest_deck_state(ctx)
    if not deck_state:
        _speak(ctx, "no deck playing")
        return Response(reply="no_deck_state", published=False)
    key = deck_state.get("key") or deck_state.get("camelot")
    if not key:
        _speak(ctx, "key unknown")
        return Response(reply="key_unknown", published=False)
    eid = _publish(
        ctx,
        "READ_KEY",
        slots={"key": key},
        transcript=intent.raw_transcript,
        confidence=intent.confidence,
    )
    _speak(ctx, f"key is {key}")
    ctx.mark_dispatch()
    return Response(reply=f"key:{key}", published=True, event_id=eid)


def handle_advance_queue(intent: Intent, ctx: VoiceContext) -> Response:
    eid = _publish(
        ctx,
        "ADVANCE_QUEUE",
        slots={},
        transcript=intent.raw_transcript,
        confidence=intent.confidence,
    )
    _speak(ctx, "advancing queue")
    ctx.mark_dispatch()
    return Response(reply="advance_queue", published=True, event_id=eid)


def handle_mute_voice(intent: Intent, ctx: VoiceContext) -> Response:
    ctx.mute()
    _speak(ctx, "voice muted for 30 minutes")
    eid = _publish(
        ctx,
        "MUTE_VOICE",
        slots={"until": ctx.mute_until},
        transcript=intent.raw_transcript,
    )
    ctx.mark_dispatch()
    return Response(reply="muted", published=True, event_id=eid)


def handle_unmute_voice(intent: Intent, ctx: VoiceContext) -> Response:
    ctx.unmute()
    ctx.clear_dispatch()
    _speak(ctx, "voice active")
    eid = _publish(
        ctx,
        "UNMUTE_VOICE",
        slots={},
        transcript=intent.raw_transcript,
    )
    return Response(reply="unmuted", published=True, event_id=eid)


# ----- Destructive handlers (Plan 2 stubs; Plan 3 real) --------------------


TRANSITION_RECENT_WINDOW_S: int = 10 * 60


def _latest_deck_state(ctx: VoiceContext) -> dict[str, Any] | None:
    recent = ctx.event_bus.recent("deck_state", limit=1)
    return recent[-1] if recent else None


def _latest_transition(
    ctx: VoiceContext, window_s: int = TRANSITION_RECENT_WINDOW_S
) -> dict[str, Any] | None:
    recent = ctx.event_bus.recent("transition", limit=1)
    if not recent:
        return None
    last = recent[-1]
    # Accept either a float ``ts_epoch`` slot or a string ``ts`` we cannot
    # reliably parse without pulling in extra deps; be lenient.
    ts_epoch = last.get("ts_epoch")
    if isinstance(ts_epoch, (int, float)):
        if time.time() - float(ts_epoch) > window_s:
            return None
    return last


def handle_save_cue(
    intent: Intent,
    ctx: VoiceContext,
    confirm_fn: Callable[[VoiceContext, str], bool] | None = None,
) -> Response:
    transition = _latest_transition(ctx)
    if transition is None:
        _speak(ctx, "no recent transition to save")
        return Response(reply="no_transition", published=False)

    if not ctx.destructive:
        eid = _publish(
            ctx,
            "SAVE_CUE",
            slots=dict(transition.get("slots") or {}),
            dry_run=True,
            transcript=intent.raw_transcript,
            confidence=intent.confidence,
            source_transition_id=transition.get("id"),
        )
        _speak(ctx, "recorded; destructive mode off")
        ctx.mark_dispatch()
        return Response(
            reply="save_cue_dry_run",
            published=True,
            event_id=eid,
            dry_run=True,
        )

    if confirm_fn is not None and not confirm_fn(
        ctx, "did you say save the last transition as cue points? yes or no"
    ):
        eid = _publish(
            ctx,
            "SAVE_CUE_CANCELLED",
            slots={},
            dry_run=True,
            transcript=intent.raw_transcript,
        )
        _speak(ctx, "cancelled")
        ctx.mark_dispatch()
        return Response(reply="save_cue_cancelled", published=True, event_id=eid)

    eid = _publish(
        ctx,
        "SAVE_CUE",
        slots=dict(transition.get("slots") or {}),
        dry_run=False,
        transcript=intent.raw_transcript,
        confidence=intent.confidence,
        source_transition_id=transition.get("id"),
        confirmed_at=time.time(),
    )
    _speak(ctx, "saved cue points")
    ctx.mark_dispatch()
    return Response(reply="save_cue_live", published=True, event_id=eid)


def handle_rate_track(
    intent: Intent,
    ctx: VoiceContext,
    confirm_fn: Callable[[VoiceContext, str], bool] | None = None,
) -> Response:
    stars_raw = intent.slots.get("stars")
    try:
        stars = int(stars_raw)
    except (TypeError, ValueError):
        _speak(ctx, "didn't catch the star count")
        return Response(reply="stars_invalid", published=False)
    if not (1 <= stars <= 5):
        _speak(ctx, "stars must be 1 to 5")
        return Response(reply="stars_out_of_range", published=False)

    deck_state = _latest_deck_state(ctx)
    if not deck_state:
        _speak(ctx, "no deck playing")
        return Response(reply="no_deck_state", published=False)
    stable_id = deck_state.get("stable_id") or deck_state.get("track_stable_id")
    if not stable_id:
        _speak(ctx, "no track identified on deck")
        return Response(reply="no_stable_id", published=False)

    if not ctx.destructive:
        eid = _publish(
            ctx,
            "RATE_TRACK",
            slots={"stable_id": stable_id, "stars": stars},
            dry_run=True,
            transcript=intent.raw_transcript,
            confidence=intent.confidence,
        )
        _speak(ctx, f"would rate {stars} stars; destructive mode off")
        ctx.mark_dispatch()
        return Response(
            reply="rate_track_dry_run",
            published=True,
            event_id=eid,
            dry_run=True,
        )

    if confirm_fn is not None and not confirm_fn(
        ctx, f"did you say rate {stars} stars? yes or no"
    ):
        eid = _publish(
            ctx,
            "RATE_TRACK_CANCELLED",
            slots={"stable_id": stable_id, "stars": stars},
            dry_run=True,
            transcript=intent.raw_transcript,
        )
        _speak(ctx, "cancelled")
        ctx.mark_dispatch()
        return Response(reply="rate_track_cancelled", published=True, event_id=eid)

    eid = _publish(
        ctx,
        "RATE_TRACK",
        slots={"stable_id": stable_id, "stars": stars},
        dry_run=False,
        transcript=intent.raw_transcript,
        confidence=intent.confidence,
        confirmed_at=time.time(),
    )
    _speak(ctx, f"rated {stars} stars")
    ctx.mark_dispatch()
    return Response(reply="rate_track_live", published=True, event_id=eid)


# ----- Registry -------------------------------------------------------------


@dataclass
class Registry:
    """Dispatch table keyed on intent kind."""

    handlers: dict[str, Handler]

    def dispatch(self, intent: Intent, ctx: VoiceContext) -> Response:
        # Respect mute / debounce BEFORE we call handlers so a muted user
        # cannot accidentally publish via a race.
        # Exceptions: UNMUTE_VOICE must work while muted.
        if ctx.is_muted() and intent.kind != "UNMUTE_VOICE":
            return Response(reply="muted", published=False, meta={"muted": True})
        if intent.kind not in DEBOUNCE_EXEMPT_INTENTS and ctx.debounced():
            return Response(
                reply="debounced", published=False, meta={"debounced": True}
            )
        handler = self.handlers.get(intent.kind)
        if handler is None:
            return Response(reply=f"no_handler:{intent.kind}", published=False)
        return handler(intent, ctx)


def default_registry() -> Registry:
    """Return the full Phase 14 dispatch table."""
    return Registry(
        handlers={
            "SEARCH": handle_search,
            "READ_BPM": handle_read_bpm,
            "READ_KEY": handle_read_key,
            "ADVANCE_QUEUE": handle_advance_queue,
            "MUTE_VOICE": handle_mute_voice,
            "UNMUTE_VOICE": handle_unmute_voice,
            "SAVE_CUE": handle_save_cue,
            "RATE_TRACK": handle_rate_track,
        }
    )
