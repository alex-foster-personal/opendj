"""LINK-24: "what is the other laptop playing", pure half (no FastAPI, no I/O).

``GET /api/v1/b2b/now-playing`` answers from the page's ui-mirror document (the
performance page publishes it about once a second), so this module only shapes
that document: one entry per loaded deck, its position and beat projected to the
moment of the answer, keys in Camelot and musical notation, and the compact
waveform overview capped to ``OVERVIEW_MAX_POINTS``.

LINK-27/28 add the mix view the other DJ needs (EQ, filter, channel fader, crossfader,
master-bus FX, post-fader peak level). Every one is optional and additive: an older
page that does not publish it yields ``None``, never a default.

Nothing here invents a value: a field the mirror does not carry is ``None``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from typing import Any

from apps.b2b_session.on_air import OnAirContext, deck_on_air
from apps.b2b_session.room_output import shape_room_output
from apps.shared.harmonic import _KEY_TO_CAMELOT, key_to_camelot
from apps.shared.link_constants import LINK

#: Hard cap on overview points per band (the deck strip uses the native 400-point PWAV).
OVERVIEW_MAX_POINTS = 2000
#: Never project a position further than this past the mirror sample (a dead page freezes).
#: LINKCONST-01: the page's PEER_MAX_PROJECT_MS, one JSON source.
MAX_PROJECT_S = LINK.peer_max_project_ms / 1000

_CAMELOT_TO_MUSICAL: dict[str, str] = {}
for _name, (_camelot_num, _letter) in _KEY_TO_CAMELOT.items():
    _CAMELOT_TO_MUSICAL.setdefault(f"{_camelot_num}{_letter}", _name)


def key_notations(key: str | None) -> tuple[str | None, str | None]:
    """``(camelot, musical)`` for a mirror key in either notation; ``(None, None)`` when unknown."""
    if key is None or not key.strip():
        return None, None
    try:
        camelot = str(key_to_camelot(key))
    except ValueError:
        return None, None
    return camelot, _CAMELOT_TO_MUSICAL[camelot]


def _downsample_max(values: list[float], cap: int) -> list[float]:
    """Per-bucket max down to ``cap`` points (same rule the anlz detail band uses)."""
    if len(values) <= cap:
        return [float(v) for v in values]
    out: list[float] = []
    n = len(values)
    for i in range(cap):
        lo, hi = (i * n) // cap, ((i + 1) * n) // cap
        out.append(float(max(values[lo:max(hi, lo + 1)])))
    return out


def shape_overview(preview: dict[str, Any], kind: str, cap: int = OVERVIEW_MAX_POINTS) -> dict[str, Any]:
    """The anlz ``waveform.preview`` bands, capped to ``cap`` points, plus a content hash."""
    bands = {name: _downsample_max(list(preview.get(name) or []), cap) for name in ("low", "mid", "high")}
    length = max(len(b) for b in bands.values())
    if length == 0:
        raise ValueError("overview has no points")
    for name, band in bands.items():
        if band and len(band) != length:
            raise ValueError(f"overview band {name} has {len(band)} points, expected {length}")
    body = {"kind": kind, "length": length, **bands}
    digest = hashlib.sha256(json.dumps(body, separators=(",", ":"), sort_keys=True).encode()).hexdigest()[:16]
    return {**body, "hash": digest}


def listing_preview_bands(preview_b64: str, preview_max: Any) -> dict[str, list[float]]:
    """Decode the listing preview (``uint8[cols][3]`` interleaved low, mid, high) to 0..1 bands."""
    raw = base64.b64decode(preview_b64)
    if len(raw) % 3:
        raise ValueError(f"listing preview has {len(raw)} bytes, not a multiple of 3")
    norm = float(preview_max) if isinstance(preview_max, (int, float)) and preview_max > 0 else 1.0
    return {name: [raw[i] / norm for i in range(offset, len(raw), 3)] for offset, name in enumerate(("low", "mid", "high"))}


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _unit(value: Any) -> float | None:
    """A 0..1 control or level; ``None`` when absent, non-numeric or out of range (never clamped into range)."""
    num = _num(value)
    return num if num is not None and 0.0 <= num <= 1.0 else None


#: Crossfader bus assignments the mixer knows (mixer-types.ts CrossfaderAssign).
_ASSIGNS = frozenset({"A", "B", "THRU"})


def shape_channel(channel: Any) -> dict[str, Any]:
    """LINK-27: one mixer strip (``mixer.channels[deck]`` in the mirror) in knob units, 0..1.

    EQ and filter 0.5 is flat or bypass, fader 1 is full. Any part the mirror does not
    carry is ``None``; a missing strip makes every part ``None``."""
    ch = channel if isinstance(channel, dict) else {}
    eq = {band: _unit(ch.get(f"eq_{band}")) for band in ("low", "mid", "high")}
    assign = ch.get("assign")
    stem_mode = ch.get("stem_eq_mode")
    return {
        "eq": None if all(v is None for v in eq.values()) else eq,
        "filter": _unit(ch.get("filter")),
        "fader": _unit(ch.get("fader")),
        "crossfader_assign": assign if assign in _ASSIGNS else None,
        "stem_eq_mode": stem_mode if isinstance(stem_mode, bool) else None,
    }


def shape_master_fx(beat_fx: Any) -> list[dict[str, Any]] | None:
    """LINK-27: the master-bus beat FX as ``[{name, on, beats, level}]``. ``[]`` when the
    peer's build has the rack off; ``None`` when the mirror does not carry ``beat_fx``."""
    if not isinstance(beat_fx, dict):
        return None
    if beat_fx.get("available") is not True:
        return []
    name = beat_fx.get("effect")
    if not isinstance(name, str) or not name:
        return None
    return [{"name": name, "on": beat_fx.get("enabled") is True, "beats": _num(beat_fx.get("beats")), "level": _unit(beat_fx.get("level"))}]


#: The roles a take-over claim may carry (link-session.svelte.ts LinkRole).
_LINK_ROLES = frozenset({"on_air", "following"})


def shape_link_claim(link_session: Any) -> dict[str, Any] | None:
    """M-72 (LINK-92): this laptop's take-over claim ``{role, took_seq, peer_id}`` from the
    mirror's ``link_session.take_over``. The other laptop compares it with its own to decide who
    stays ON AIR. ``None`` when the page publishes none or a malformed one (never a default role)."""
    claim = link_session.get("take_over") if isinstance(link_session, dict) else None
    if not isinstance(claim, dict) or claim.get("role") not in _LINK_ROLES:
        return None
    # took_seq: a Lamport take-over number (two Macs share no clock to timestamp a press with).
    stamp, peer_id = claim.get("took_seq"), claim.get("peer_id")
    if stamp is not None and (isinstance(stamp, bool) or not isinstance(stamp, int)):
        return None
    if peer_id is not None and not isinstance(peer_id, str):
        return None
    return {"role": claim["role"], "took_seq": stamp, "peer_id": peer_id}


def shape_link_phrase(link_session: Any) -> dict[str, Any] | None:
    """LINK-113: the peer's OWN LINK lane grid from the mirror's ``link_session.phrase``, sampled
    when the mirror was published: ``beat_in_phrase`` (beats into its phrase, [0, 16)),
    ``bar_phase_beats`` (the phrase anchor's beat within the Link bar, [0, 4)) and ``source``
    (``master``: its MASTER deck's beatgrid, ``link``: its Link count). The other laptop maps it
    onto its own Link count and both pick the leading laptop's MASTER grid. ``None`` when the page
    publishes none (no clock, an older page) or any field is missing or malformed: never a default."""
    phrase = link_session.get("phrase") if isinstance(link_session, dict) else None
    if not isinstance(phrase, dict):
        return None
    beat, bar_phase, source = phrase.get("beat_in_phrase"), phrase.get("bar_phase_beats"), phrase.get("source")
    if not (_finite_in(beat, 16) and _finite_in(bar_phase, 4)) or source not in ("master", "link"):
        return None
    return {"beat_in_phrase": float(beat), "bar_phase_beats": float(bar_phase), "source": source}


#: AUDIOCLOCK-03 watchdog states (audio-clock-watchdog.ts AudioClockState).
_AUDIO_CLOCK_STATES = frozenset({"unmeasured", "healthy", "suspect", "mispaced", "recovering", "failed"})


def shape_audio_clock(audio_health: Any) -> dict[str, Any] | None:
    """LINKCLOCK-02 (audit #6646, the clock-rate watchdog gap): this laptop's audio clock watchdog
    as ``{state, recovery_step, reason}``, so the other laptop can say "their audio clock is being
    rebuilt" instead of showing drift with no reason. ``None`` from an older page or a malformed one."""
    clock = audio_health.get("clock") if isinstance(audio_health, dict) else None
    if not isinstance(clock, dict) or clock.get("state") not in _AUDIO_CLOCK_STATES:
        return None
    step, reason = clock.get("recovery_step"), clock.get("reason")
    return {
        "state": clock["state"],
        "recovery_step": step if isinstance(step, str) else None,
        "reason": reason if isinstance(reason, str) else None,
    }


def _finite_in(value: Any, upper: float) -> bool:
    """A real finite number in [0, upper); a bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    return 0 <= value < upper


def shape_deck(
    deck_id: str, deck: dict[str, Any], mirror_age_s: float, channel: Any, on_air_context: OnAirContext
) -> dict[str, Any] | None:
    """One loaded deck from a ui-mirror ``decks`` entry, projected ``mirror_age_s`` forward
    when playing (at the deck's effective tempo ratio). ``None`` for an empty deck.
    ``channel`` is the deck's mixer strip from the mirror (``None`` on an older page)."""
    stable_id = deck.get("stable_id")
    if not isinstance(stable_id, str) or not stable_id:
        return None
    playing = deck.get("playing") is True
    bpm, current_bpm = _num(deck.get("bpm")), _num(deck.get("effective_bpm"))
    rate = current_bpm / bpm if bpm and current_bpm else 1.0
    position_in = deck.get("position")
    position: dict[str, Any] = position_in if isinstance(position_in, dict) else {}
    position_ms = _num(position.get("ms"))
    duration_ms = _num(deck.get("duration_ms"))
    beat = _num(deck.get("beat_at_position"))
    age = min(max(mirror_age_s, 0.0), MAX_PROJECT_S) if playing else 0.0
    position_s = None if position_ms is None else position_ms / 1000.0 + age * rate
    if position_s is not None and duration_ms is not None:
        position_s = min(position_s, duration_ms / 1000.0)
    if beat is not None and current_bpm:
        beat += age * current_bpm / 60.0
    camelot, musical = key_notations(deck.get("key") if isinstance(deck.get("key"), str) else None)
    return {
        "deck": int(deck_id),
        "playing": playing,
        "stable_id": stable_id,
        "content_hash": deck.get("content_hash") if isinstance(deck.get("content_hash"), str) else None,
        "title": deck.get("title"),
        "artist": deck.get("artist"),
        "key_camelot": camelot,
        "key_musical": musical,
        "bpm": bpm,
        "current_bpm": current_bpm,
        "duration_s": None if duration_ms is None else duration_ms / 1000.0,
        "position_s": position_s,
        "beat": beat,
        "is_master": deck.get("is_master") is True,
        **shape_channel(channel),
        # LINK-27: post-EQ, post-fader held peak, linear 0..1; None when the page has no audio graph.
        "level": _unit(deck.get("level_peak")),
        # LINKAIR-01: this engine's ON AIR verdict for its own deck (the other laptop renders it).
        **_on_air_fields(channel, on_air_context),
    }


def _on_air_fields(channel: Any, context: OnAirContext) -> dict[str, Any]:
    gain = _unit(channel.get("on_air_gain")) if isinstance(channel, dict) else None
    verdict = deck_on_air(loaded=True, gain=gain, context=context)
    return {"on_air": verdict.on_air, "on_air_reason": verdict.reason}


def shape_now_playing(
    mirror: dict[str, Any], mirror_age_s: float, machine: str, max_mirror_age_s: float, on_air_context: OnAirContext
) -> dict[str, Any]:
    """The now-playing answer for one mirror document. ``stale`` when the page has not
    published for ``max_mirror_age_s`` (its positions are then frozen, never run on)."""
    decks_in = mirror.get("decks")
    if not isinstance(decks_in, dict):
        raise TypeError("ui-mirror has no decks object")
    master = mirror.get("master_deck")
    mixer_in = mirror.get("mixer")
    mixer: dict[str, Any] = mixer_in if isinstance(mixer_in, dict) else {}
    channels_in = mixer.get("channels")
    channels: dict[str, Any] = channels_in if isinstance(channels_in, dict) else {}
    decks = []
    for deck_id, deck in sorted(decks_in.items()):
        if not isinstance(deck, dict):
            continue
        shaped = shape_deck(
            deck_id, {**deck, "is_master": str(master) == str(deck_id)}, mirror_age_s, channels.get(str(deck_id)), on_air_context
        )
        if shaped is not None:
            decks.append(shaped)
    return {
        "machine": machine,
        "mirror_age_ms": round(mirror_age_s * 1000.0, 1),
        "stale": mirror_age_s > max_mirror_age_s,
        "master_deck": master if isinstance(master, int) else None,
        # LINK-27: crossfader 0 = full A, 1 = full B; master-bus FX. None on an older page.
        "crossfader": _unit(mixer.get("crossfader")),
        "master_fx": shape_master_fx(mirror.get("beat_fx")),
        # LINK-92: who claims ON AIR and since when (Link clock), for the take-over clash rule.
        "link": shape_link_claim(mirror.get("link_session")),
        # P2-10: where the ON AIR laptop is in the session phrase; None unless it is ON AIR.
        "link_phrase": shape_link_phrase(mirror.get("link_session")),
        # B2BROOM-02: does this laptop play to the room ({speakers_on, room_path}); None on an older page.
        "room": shape_room_output(mirror.get("room_output")),
        # LINKCLOCK-02: this laptop's audio clock watchdog, so the partner can name a rebuild.
        "audio_clock": shape_audio_clock(mirror.get("audio_health")),
        "decks": decks,
    }
