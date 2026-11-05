"""B2BAUDIO-01..06, -09..-11, -13..-15, -17, -18: partner audio, pure half (no FastAPI, no I/O).

A stopgap until the shared booth (every laptop renders all four decks): the paired partner's
page streams its MASTER bus to this laptop over a WebRTC data channel (LAN host candidates only), and this
laptop plays it into its own room output and delays its own master to the room by the measured
stream latency, so the two DJs' beats land together in the room.

Signalling rides the existing paired B2B door with no new write path on it: each engine keeps an
**outbox** of its page's signalling messages (``hello``, ``offer``, ``answer``, ``ice``, ``bye``),
and the partner engine READS it through the door (``GET /api/v1/b2b/partner-audio/outbox``,
allowlisted in ``peer_listener.ALLOWED`` with the pairing token), exactly as shouts do. The page
reads the partner's outbox through its own engine (``/inbox``), which rewrites the partner's
mDNS-obfuscated host candidates (``<uuid>.local``) to the partner's LAN address from the
pairing, so ICE needs no multicast DNS on a venue network that blocks it.

This module holds what can be decided without I/O: message validation, the outbox ring, the
candidate rewrite, the intent record and the shape of the status the page publishes.
"""

from __future__ import annotations

import re
import secrets
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

from apps.shared.link_constants import LINK

KINDS: frozenset[str] = frozenset({"hello", "offer", "answer", "ice", "bye"})
SignalKind = Literal["hello", "offer", "answer", "ice", "bye"]
#: Session ids: the receiver's ``rid`` names one receive attempt, the sender's ``sid`` one offer.
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
#: A data-channel offer with a handful of host candidates is about 1 KiB; anything near this is not one.
MAX_SDP_BYTES = 16 * 1024
MAX_CANDIDATE_BYTES = 1024
#: The outbox keeps only the newest messages: signalling is a handful per (re)connect.
OUTBOX_LEN = 64
#: Gain is linear amplitude on the partner stream, 0 (silent) .. 2 (+6 dB).
GAIN_MIN, GAIN_MAX = 0.0, 2.0
#: With no intent stored: this laptop serves its master when the partner asks (send on), does
#: not pull the partner's (receive off), at unity gain. Receive is the one click that starts it.
DEFAULT_SEND, DEFAULT_RECEIVE, DEFAULT_GAIN = True, False, 1.0

#: ``a=candidate:<foundation> <component> <proto> <priority> <address> <port> typ <type> ...``
_CANDIDATE_RE = re.compile(
    r"^(?P<head>(?:a=)?candidate:\S+ \d+ \S+ \d+ )(?P<address>\S+)(?P<tail> \d+ typ \S+.*)$"
)
_MDNS_HOST_RE = re.compile(r"^[0-9a-fA-F-]{8,64}\.local$")


class SignalError(ValueError):
    """A signalling message the engine refuses; ``code`` names why (422 on the wire)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- validation


def _require_id(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise SignalError("bad_id", f"{key} must match {ID_RE.pattern}, got {value!r}")
    return value


def _require_sdp(body: dict[str, Any], kind: str) -> str:
    sdp = body.get("sdp")
    if not isinstance(sdp, str) or not sdp.startswith("v=0"):
        raise SignalError("bad_sdp", f"an {kind} needs an sdp string starting 'v=0'")
    if len(sdp.encode()) > MAX_SDP_BYTES:
        raise SignalError("sdp_too_large", f"sdp is {len(sdp.encode())} bytes, over {MAX_SDP_BYTES}")
    if "m=application" not in sdp:
        raise SignalError("bad_sdp", f"an {kind} must carry an m=application (data channel) section")
    return sdp


def validate_signal(body: Any) -> dict[str, Any]:
    """The message as stored, or ``SignalError``. Shapes, by kind:

    - ``hello``  ``{rid}``                       a receiver asking for the stream (each attempt a new rid)
    - ``offer``  ``{rid, sid, sdp}``             the sender's offer, answering that hello
    - ``answer`` ``{rid, sid, sdp}``             the receiver's answer to that offer
    - ``ice``    ``{rid, sid, candidate}``       one trickled candidate (``""`` ends the trickle)
    - ``bye``    ``{rid, sid?}``                 either side stopping that session

    Unknown keys are refused rather than stored: a page and an engine that disagree on the
    message shape must fail at the engine, not as a silent half-negotiated call.
    """
    if not isinstance(body, dict):
        raise SignalError("bad_message", "a signal must be a JSON object")
    kind = body.get("kind")
    if kind not in KINDS:
        raise SignalError("bad_kind", f"kind must be one of {sorted(KINDS)}, got {kind!r}")
    allowed = {"hello": {"kind", "rid"}, "offer": {"kind", "rid", "sid", "sdp"},
               "answer": {"kind", "rid", "sid", "sdp"}, "ice": {"kind", "rid", "sid", "candidate"},
               "bye": {"kind", "rid", "sid"}}[kind]
    extra = sorted(set(body) - allowed)
    if extra:
        raise SignalError("bad_message", f"unexpected keys for {kind}: {extra}")
    out: dict[str, Any] = {"kind": kind, "rid": _require_id(body, "rid")}
    if kind in ("offer", "answer", "ice") or (kind == "bye" and body.get("sid") is not None):
        out["sid"] = _require_id(body, "sid")
    if kind in ("offer", "answer"):
        out["sdp"] = _require_sdp(body, kind)
    elif kind == "ice":
        candidate = body.get("candidate")
        if not isinstance(candidate, str) or len(candidate.encode()) > MAX_CANDIDATE_BYTES:
            raise SignalError("bad_candidate", f"candidate must be a string up to {MAX_CANDIDATE_BYTES} bytes")
        if candidate and not _CANDIDATE_RE.match(candidate):
            raise SignalError("bad_candidate", f"not an ICE candidate line: {candidate[:120]!r}")
        out["candidate"] = candidate
    return out


# ---------------------------------------------------------------- outbox


@dataclass
class Outbox:
    """This engine's signalling messages, newest ``OUTBOX_LEN`` kept, each with a ``seq``.

    ``epoch`` changes on every engine start, so a reader holding a ``since`` from a previous
    engine life starts over instead of waiting forever for a seq that was reset.
    """

    epoch: str = field(default_factory=lambda: secrets.token_hex(8))
    _seq: int = 0
    _items: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=OUTBOX_LEN))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def post(self, message: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            stored = {**message, "seq": self._seq, "age_origin": monotonic()}
            self._items.append(stored)
            return self._public(stored)

    def since(self, seq: int) -> dict[str, Any]:
        if seq < 0:
            raise SignalError("bad_since", f"since must be >= 0, got {seq}")
        with self._lock:
            messages = [self._public(item) for item in self._items if item["seq"] > seq]
            return {"epoch": self.epoch, "seq": self._seq, "messages": messages}

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in item.items() if k != "age_origin"}
        out["age_ms"] = round((monotonic() - item["age_origin"]) * 1000.0)
        return out


# ---------------------------------------------------------------- candidate rewrite


def rewrite_candidate(candidate: str, peer_host: str) -> str:
    """Replace an mDNS host candidate's ``<uuid>.local`` address with the partner's LAN address.

    Browsers hide host IPs behind mDNS names; resolving them needs multicast DNS, which venue
    Wi-Fi often blocks (B2BMCAST). The pairing already knows the partner's address, so the
    engine substitutes it. Any other candidate (an IP, a srflx) passes through untouched."""
    match = _CANDIDATE_RE.match(candidate)
    if match is None or not _MDNS_HOST_RE.fullmatch(match["address"]):
        return candidate
    return f"{match['head']}{peer_host}{match['tail']}"


def rewrite_sdp(sdp: str, peer_host: str) -> str:
    """``rewrite_candidate`` on every ``a=candidate:`` line, line endings preserved."""
    lines = sdp.split("\r\n") if "\r\n" in sdp else sdp.split("\n")
    sep = "\r\n" if "\r\n" in sdp else "\n"
    return sep.join(rewrite_candidate(line, peer_host) if line.startswith("a=candidate:") else line for line in lines)


def rewrite_message(message: dict[str, Any], peer_host: str) -> dict[str, Any]:
    out = dict(message)
    if isinstance(out.get("sdp"), str):
        out["sdp"] = rewrite_sdp(out["sdp"], peer_host)
    if isinstance(out.get("candidate"), str) and out["candidate"]:
        out["candidate"] = rewrite_candidate(out["candidate"], peer_host)
    return out


# ---------------------------------------------------------------- intent


@dataclass
class Intent:
    """What this laptop WANTS (the panel, CLI and MCP write it; the page obeys within a poll)."""

    send: bool = DEFAULT_SEND
    receive: bool = DEFAULT_RECEIVE
    gain: float = DEFAULT_GAIN
    revision: int = 0

    def update(self, send: bool | None, receive: bool | None, gain: float | None) -> None:
        if send is not None:
            self.send = send
        if receive is not None:
            self.receive = receive
        if gain is not None:
            if not GAIN_MIN <= gain <= GAIN_MAX:
                raise SignalError("bad_gain", f"gain must be within {GAIN_MIN}..{GAIN_MAX}, got {gain}")
            self.gain = float(gain)
        self.revision += 1

    def as_dict(self) -> dict[str, Any]:
        return {"send": self.send, "receive": self.receive, "gain": self.gain, "revision": self.revision}


# ---------------------------------------------------------------- applied status (from the page)

_SIDE_KEYS = ("state", "connected", "reconnects")
_RECEIVE_COUNTERS = ("graph_reattaches", "resyncs")
_RECEIVE_NUMBERS = (
    "latency_ms",
    "applied_delay_ms",
    "jitter_buffer_ms",
    "jitter_target_ms",
    "measured_latency_ms",
    "clock_rtt_ms",
    # B2BAUDIO-09: the partner's clock minus this one, how much its samples disagree, how far off
    # it can be; and the stream's level as it arrives (before the fader, master volume and mute).
    "clock_offset_ms",
    "clock_offset_jitter_ms",
    "clock_offset_bound_ms",
    "level_peak_dbfs",
    "level_rms_dbfs",
    "rtt_ms",
    "packet_loss_pct",
    "packets_lost",
    "packets_received",
    "concealment_events",
    "gain",
    # B2BAUDIO-13: the delay the network asks for, the slowest packet's arrival delay in the last
    # second, and the longest wait between two packets off the network.
    "jitter_wanted_ms",
    "arrival_delay_ms",
    "network_gap_ms",
    # B2BAUDIO-14: ``packets_lost`` by cause: arrived after their play time, or never in time at all.
    "packets_late",
    "packets_lost_network",
    # B2BAUDIO-21: concealed audio that arrived before its play time and was dropped unplayed.
    "packets_discarded",
    # B2BAUDIO-22: the share (percent) of the last seconds of output that was real audio.
    "played_pct",
    # B2BAUDIO-20: each laptop's audio output latency as its browser reports it.
    "output_latency_ms",
    "partner_output_latency_ms",
    # B2BAUDIO-17: the stream as it leaves for the room (after the fader, the Speakers switch,
    # master volume and master mute; -120 is silence), and latency less the applied room delay.
    "room_level_peak_dbfs",
    "room_level_rms_dbfs",
    "room_alignment_ms",
)
#: B2BAUDIO-15, -19: the sender's codec bookkeeping and the cadence of its own audio thread.
_SEND_NUMBERS = ("codec_changes", "codec_clean_s", "codec_retry_after_s", "feed_gap_ms", "buffered_peak_bytes")
#: B2BAUDIO-22: under this share (percent) of real audio in its output a stream is not playing
#: (the page's PLAYED_SHARE_MIN, in percent; LINKCONST-01: one JSON source).
PLAYED_PCT_MIN = LINK.played_share_min * 100
#: B2BAUDIO-13: what the target delay is doing.
TARGET_STATES: frozenset[str] = frozenset({"preroll", "raise", "lower", "hold"})
#: A reason or an error from the page is a sentence, not a document.
MAX_REASON_CHARS = 300
_SIGNAL_NUMBERS = ("failures", "failing_for_ms", "failures_total", "door_rtt_ms")


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value[:MAX_REASON_CHARS] if isinstance(value, str) else None


def shape_signal(value: Any) -> dict[str, Any] | None:
    """B2BAUDIO-18: the page's signalling-poll health (the partner's door), or None when the page
    publishes none (an older build). A value of the wrong type is None, never passed through."""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {"ok": value.get("ok") if isinstance(value.get("ok"), bool) else None}
    for key in _SIGNAL_NUMBERS:
        out[key] = _number(value.get(key))
    out["last_error"] = _text(value.get("last_error"))
    return out


def shape_applied(mirror_value: Any) -> dict[str, Any] | None:
    """The ui-mirror's ``partner_audio`` as published by the page, or None when the page does not
    publish a well-formed one (no page, an older build). Numbers stay None when the page has
    none yet; nothing here invents a value."""
    if not isinstance(mirror_value, dict):
        return None
    send, receive = mirror_value.get("send"), mirror_value.get("receive")
    if not isinstance(send, dict) or not isinstance(receive, dict):
        return None
    for side in (send, receive):
        if not isinstance(side.get("state"), str) or not isinstance(side.get("connected"), bool):
            return None
    out_receive = {key: receive.get(key) for key in _SIDE_KEYS + _RECEIVE_COUNTERS}
    for key in _RECEIVE_NUMBERS:
        out_receive[key] = _number(receive.get(key))
    out_receive["target_state"] = receive.get("target_state") if receive.get("target_state") in TARGET_STATES else None
    out_receive["error"] = receive.get("error") if isinstance(receive.get("error"), str) else None
    source = receive.get("latency_source")
    out_receive["latency_source"] = source if source in ("measured", "estimated") else None
    out_send = {key: send.get(key) for key in _SIDE_KEYS}
    for side_in, side_out in ((receive, out_receive), (send, out_send)):
        # Where the packets are relayed (off the page's thread or on it) and how they are coded.
        side_out["relay"] = side_in.get("relay") if side_in.get("relay") in ("worker", "main") else None
        side_out["codec"] = side_in.get("codec") if side_in.get("codec") in ("pcm", "opus") else None
    for key in ("skipped_packets", "uncopied_packets"):
        value = send.get(key)
        out_send[key] = value if isinstance(value, int) and not isinstance(value, bool) else None
    out_send["bitrate_kbps"] = send.get("bitrate_kbps") if isinstance(send.get("bitrate_kbps"), (int, float)) else None
    out_send["error"] = send.get("error") if isinstance(send.get("error"), str) else None
    for key in _SEND_NUMBERS:
        out_send[key] = _number(send.get(key))
    # B2BAUDIO-15: why the stream is on its codec (the sender decides; it tells the receiver).
    out_send["codec_reason"] = _text(send.get("codec_reason"))
    #: Whether the PCM-sized test load is being sent beside the Opus stream (the way back to PCM).
    out_send["codec_canary"] = send.get("codec_canary") if isinstance(send.get("codec_canary"), bool) else None
    out_receive["codec_reason"] = _text(receive.get("codec_reason"))
    # B2BAUDIO-10: what each side is waiting on (a named reason, never free text), and the
    # receiver's words for it.
    for side_in, side_out in ((receive, out_receive), (send, out_send)):
        side_out["waiting_on"] = side_in.get("waiting_on") if side_in.get("waiting_on") in WAITING_ON else None
    out_receive["notice"] = receive.get("notice") if isinstance(receive.get("notice"), str) else None
    return {"send": out_send, "receive": out_receive, "signal": shape_signal(mirror_value.get("signal"))}


#: B2BAUDIO-10: ``audio_context_suspended`` is this laptop's own audio not started (no click on
#: the page yet); the ``partner_`` forms are the receiver's view of the other laptop.
WAITING_ON: frozenset[str] = frozenset(
    {"audio_context_suspended", "partner_audio_context_suspended", "partner_not_answering"}
)


def page_hidden_of(mirror_value: Any) -> bool | None:
    """Whether the page said its tab was hidden when it wrote this status; None when it did not say."""
    hidden = mirror_value.get("page_hidden") if isinstance(mirror_value, dict) else None
    return hidden if isinstance(hidden, bool) else None


# ---------------------------------------------------------------- how old the applied status is


@dataclass(frozen=True)
class Published:
    """One thing the page published: the shaped status, when (``monotonic()``), and by which
    road: ``status`` is the page's own once-a-second post, ``mirror`` the ui-mirror."""

    applied: dict[str, Any]
    at: float
    source: Literal["status", "mirror"]
    page_hidden: bool | None


NEVER_PUBLISHED_NOTE = (
    "no page has published a partner-audio status since this engine started (is the performance page open?)"
)


def applied_view(published: Sequence[Published], now: float, max_age_s: float) -> dict[str, Any]:
    """B2BAUDIO-11: what the page applied, by the freshest road, with its age, and never a silent
    null. ``applied`` is the status when it is at most ``max_age_s`` old; older than that it is
    None (a stale "connected" must not read as now), ``last_applied`` still carries it, and
    ``applied_note`` says why in words: ``page hidden, status N s old`` when the page said its tab
    was hidden (a hidden tab publishes about once a minute), else that the page went quiet."""
    if not published:
        return {"applied": None, "applied_source": None, "stale_ms": None, "page_hidden": None,
                "applied_note": NEVER_PUBLISHED_NOTE, "last_applied": None}
    newest = max(published, key=lambda item: item.at)
    age_s = max(0.0, now - newest.at)
    out: dict[str, Any] = {"applied_source": newest.source, "stale_ms": round(age_s * 1000.0),
                           "page_hidden": newest.page_hidden}
    if age_s <= max_age_s:
        return {**out, "applied": newest.applied, "applied_note": None, "last_applied": None}
    note = (f"page hidden, status {age_s:.0f} s old" if newest.page_hidden
            else f"no status from the page for {age_s:.0f} s (page closed, reloading or stalled)")
    return {**out, "applied": None, "applied_note": note, "last_applied": newest.applied}
