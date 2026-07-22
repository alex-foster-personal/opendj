"""Voice probe endpoint (text-command-entry).

Wires the apps/voice text-in -> deterministic grammar -> intent-out probe
path (RECON-FEATURES.md apps/voice section) behind a small HTTP surface so
the /performance UI can drive voice intents without a mic or wake-word
daemon. Destructive intents (SAVE_CUE, RATE_TRACK) are never dispatched
from this endpoint - they are reported blocked instead, matching the
"probe, no mic" contract described in apps/voice/README.md.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/voice", tags=["voice"])

DESTRUCTIVE_INTENTS: frozenset[str] = frozenset({"SAVE_CUE", "RATE_TRACK"})


class VoiceProbeRequest(BaseModel):
    text: str


class VoiceProbeResponse(BaseModel):
    transcript: str
    intent: str | None
    slots: dict[str, Any] = {}
    blocked: bool = False
    reason: str | None = None
    reply: str | None = None
    client_action: Literal["browser_search"] | None = None
    probe_only: bool = True


def _import_voice_stack():
    """Import the apps/voice modules the probe path needs.

    Kept as its own function (instead of inline imports) so tests can
    monkeypatch it to simulate a missing optional dependency without
    actually uninstalling anything.
    """
    from apps.voice import actions, bus, context as ctx_mod, grammar, tts

    return grammar, actions, bus, ctx_mod, tts


@router.post("/probe", response_model=VoiceProbeResponse)
def probe(body: VoiceProbeRequest) -> VoiceProbeResponse | JSONResponse:
    try:
        grammar, actions, bus, ctx_mod, tts = _import_voice_stack()
    except ImportError as exc:
        missing = exc.name or str(exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "voice_probe_unavailable",
                    "message": f"apps.voice unavailable: missing dependency '{missing}'",
                }
            },
        )

    intent = grammar.parse(body.text)
    if intent is None:
        return VoiceProbeResponse(
            transcript=body.text, intent=None, reason="grammar_miss"
        )

    if intent.kind in DESTRUCTIVE_INTENTS:
        return VoiceProbeResponse(
            transcript=body.text,
            intent=intent.kind,
            slots=intent.slots,
            blocked=True,
            reason=(
                "destructive intents are not executed via the text probe "
                "endpoint; use the voice daemon with --enable-destructive"
            ),
            probe_only=True,
        )

    # Fresh, disposable context per request. InMemoryBus is deliberately
    # different from make_bus(force_stub=True), whose JsonlStubBus persists
    # transcripts under data/voice/. A probe must never touch disk or shared
    # state. SEARCH is returned as an explicit client action; every other
    # non-destructive intent remains an observable, side-effect-free probe.
    event_bus = bus.InMemoryBus()
    ctx = ctx_mod.VoiceContext.from_env(
        event_bus=event_bus, tts_engine=tts.RecordingTts()
    )
    registry = actions.default_registry()
    response = registry.dispatch(intent, ctx)

    return VoiceProbeResponse(
        transcript=body.text,
        intent=intent.kind,
        slots=intent.slots,
        blocked=False,
        reply=response.reply,
        client_action="browser_search" if intent.kind == "SEARCH" else None,
        probe_only=intent.kind != "SEARCH",
    )
