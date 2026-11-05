"""Shared booth wire shapes (B2BBOOTH-01, B2BBOOTH-12, B2BBOOTH-90): pure validation, no I/O.

Contract: docs/b2b-link/BOOTH-CONTRACT.md v3, "Messages". The ENGINE validates everything that
reaches it, from its own page (``validate_intent``) and from the partner's engine
(``validate_envelope``); anything the contract does not name is refused with a named code and
never stored or applied.

Engine-to-engine envelope (one door POST on a lane, a contract choice listed in the S1b PR):
``{v: 1, epoch, sent_us, messages: [{kind, ...}]}``. ``epoch`` may be null only on the joiner's
first ``resend {from: 1}`` (it has no epoch until the inviter's snapshot names one).

OPS lane kinds: ``propose {from, pid, op, trace?}``, ``commit {ops}``, ``resend {from}``,
``snapshot {ops}``. CTL lane kinds: ``ctl {side, unit, ctl, value, cseq, at_link_us, trace?}``,
``pos {deck, pos_frames, at_link_us, rate}``, ``renew {side, units, at_link_us}``, and on the door's
ctl lane only ``udp {port, sid}`` (B2BBOOTH-97: this engine's sealed datagram socket for the epoch).
DGRAM lane (B2BBOOTH-97, sealed UDP on a LAN, same envelope): ``ctl``, ``pos``, ``renew`` plus
``ping {t0_us}`` / ``pong {t0_us, now_us}`` (the datagram path's probe and clock samples).
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal

Side = Literal["inviter", "joiner"]
Role = Literal["audio", "control"]
#: ``ops`` and ``ctl`` ride the paired door; ``dgram`` is the sealed UDP lane on a LAN (B2BBOOTH-97).
Lane = Literal["ops", "ctl", "dgram"]

PROTOCOL_VERSION = 1
SIDES: tuple[Side, Side] = ("inviter", "joiner")
DECKS: tuple[int, ...] = (1, 2, 3, 4)
UNITS: tuple[str, ...] = (*(f"deck:{d}" for d in DECKS), *(f"strip:{d}" for d in DECKS), "xfader", "master")
#: The door lanes (``POST /b2b-booth/<lane>``).
LANES: tuple[Lane, Lane] = ("ops", "ctl")
#: B2BBOOTH-121: the one-shot front-door POST (``POST /b2b-booth/invite``): the partner who pressed
#: "Start shared booth" asks this engine to open its side. Not a lane: no epoch, no ordering.
INVITE_PATH = "invite"
EPOCH_RE = re.compile(r"^[0-9a-f]{16}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: Continuous controls that travel (contract v3 "Messages"); ``trim`` added in S1b.
CTL_NAME_RE = re.compile(r"^(tempo|pitch_bend|jog|trim|eq\.(low|mid|high)|filter|fader|xfader|master_gain|fx\.[0-9]{1,2}\.(on|amount))$")
#: Each laptop's own outputs: never sent, refused with ``local_only`` if a page tries.
LOCAL_ONLY_CTLS = frozenset({"cue_bus", "cue_volume", "cue_mix", "master_out_level"})
DECK_OPS = frozenset({"load", "play", "pause", "cue", "seek", "hotcue", "loop", "loop_off"})
POSITIONED_OPS = frozenset({"play", "pause", "cue", "seek", "hotcue", "loop"})
LEASE_OPS = frozenset({"grab", "yield", "refuse", "release", "locked"})
SETTING_KEYS = ("lock_half_on_turn", "audio_laptop", "two_outputs")
OPS_KINDS = frozenset({"propose", "commit", "resend", "snapshot"})
CTL_KINDS = frozenset({"ctl", "pos", "renew", "udp"})
DGRAM_KINDS = frozenset({"ctl", "pos", "renew", "ping", "pong"})
_KINDS_BY_LANE = {"ops": OPS_KINDS, "ctl": CTL_KINDS, "dgram": DGRAM_KINDS}
SID_RE = re.compile(r"^[0-9a-f]{16}$")
TRACE_KEYS = frozenset({"page_us", "engine_us", "inviter_us"})


class BoothError(ValueError):
    """A request the booth refuses; ``code`` names why, ``status`` is the HTTP status."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def other_side(side: Side) -> Side:
    return "joiner" if side == "inviter" else "inviter"


def units_of_half(side: Side) -> list[str]:
    decks = (1, 2) if side == "inviter" else (3, 4)
    return [u for d in decks for u in (f"deck:{d}", f"strip:{d}")]


def half_of_unit(unit: str) -> Side | None:
    if unit in ("xfader", "master"):
        return None
    return "inviter" if int(unit.split(":")[1]) <= 2 else "joiner"


def unit_of_deck_op(op: dict[str, Any]) -> str | None:
    return f"deck:{op['deck']}" if op["op"] in DECK_OPS else None


# ---------------------------------------------------------------- field helpers


def _only(obj: dict[str, Any], allowed: set[str] | frozenset[str], where: str) -> None:
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise BoothError("extra_keys", f"unexpected keys for {where}: {extra}")


def _obj(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BoothError("bad_field", f"{where} must be a JSON object")
    return value


def _int(obj: dict[str, Any], key: str, minimum: int, where: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise BoothError("bad_field", f"{where}.{key} must be an integer >= {minimum}, got {value!r}")
    return value


def _num(obj: dict[str, Any], key: str, where: str) -> float:
    value = obj.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise BoothError("bad_field", f"{where}.{key} must be a finite number, got {value!r}")
    return value


def _str(obj: dict[str, Any], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise BoothError("bad_field", f"{where}.{key} must be a string, got {value!r}")
    return value


def _bool(obj: dict[str, Any], key: str, where: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise BoothError("bad_field", f"{where}.{key} must be a boolean, got {value!r}")
    return value


def side_field(obj: dict[str, Any], key: str, where: str) -> Side:
    value = obj.get(key)
    if value not in SIDES:
        raise BoothError("bad_field", f"{where}.{key} must be inviter or joiner, got {value!r}")
    return value  # type: ignore[return-value]


def unit_field(obj: dict[str, Any], where: str) -> str:
    value = obj.get("unit")
    if value not in UNITS:
        raise BoothError("bad_field", f"{where}.unit must be one of {'|'.join(UNITS)}, got {value!r}")
    return value  # type: ignore[return-value]


def deck_field(obj: dict[str, Any], where: str) -> int:
    value = obj.get("deck")
    if value not in DECKS or isinstance(value, bool):
        raise BoothError("bad_field", f"{where}.deck must be 1..4, got {value!r}")
    return value  # type: ignore[return-value]


def epoch_field(value: Any, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not EPOCH_RE.fullmatch(value):
        raise BoothError("bad_epoch", f"epoch must be 16 lowercase hex, got {value!r}")
    return value


# ---------------------------------------------------------------- discrete ops


def _track(value: Any) -> dict[str, Any]:
    where = "load.track"
    value = _obj(value, where)
    _only(value, {"sha256", "bytes", "title", "artist", "bpm", "key", "duration_s", "owner_track_id"}, where)
    sha = _str(value, "sha256", where)
    if not SHA256_RE.fullmatch(sha):
        raise BoothError("bad_field", f"{where}.sha256 must be 64 lowercase hex")
    return {
        "sha256": sha,
        "bytes": _int(value, "bytes", 1, where),
        "title": _str(value, "title", where),
        "artist": _str(value, "artist", where),
        "bpm": None if value.get("bpm") is None else _num(value, "bpm", where),
        "key": None if value.get("key") is None else _str(value, "key", where),
        "duration_s": _num(value, "duration_s", where),
        "owner_track_id": _str(value, "owner_track_id", where),
    }


def _v_load(value: dict[str, Any], where: str) -> dict[str, Any]:
    _only(value, {"op", "deck", "track"}, where)
    return {"op": "load", "deck": deck_field(value, where), "track": _track(value.get("track"))}


def _v_positioned(value: dict[str, Any], where: str) -> dict[str, Any]:
    name = value["op"]
    _only(value, {"op", "deck", "at_link_us", "pos_frames"} | ({"loop"} if name == "loop" else set()), where)
    out: dict[str, Any] = {
        "op": name,
        "deck": deck_field(value, where),
        "at_link_us": _int(value, "at_link_us", 0, where),
        "pos_frames": _num(value, "pos_frames", where),
    }
    if name == "loop":
        loop = _obj(value.get("loop"), f"{where}.loop")
        _only(loop, {"in", "out"}, f"{where}.loop")
        loop_in, loop_out = _num(loop, "in", f"{where}.loop"), _num(loop, "out", f"{where}.loop")
        if not loop_out > loop_in:
            raise BoothError("bad_field", f"{where}.loop.out must be after loop.in")
        out["loop"] = {"in": loop_in, "out": loop_out}
    return out


def _v_loop_off(value: dict[str, Any], where: str) -> dict[str, Any]:
    _only(value, {"op", "deck"}, where)
    return {"op": "loop_off", "deck": deck_field(value, where)}


def _v_grab(value: dict[str, Any], where: str) -> dict[str, Any]:
    _only(value, {"op", "unit", "phase", "reason"}, where)
    phase = value.get("phase")
    if phase not in ("ask", "granted"):
        raise BoothError("bad_field", f"{where}.phase must be ask or granted")
    out = {"op": "grab", "unit": unit_field(value, where), "phase": phase}
    if "reason" in value:
        if value["reason"] not in ("unheld", "lapsed", "yield", "silence") or phase != "granted":
            raise BoothError("bad_field", f"{where}.reason must be unheld|lapsed|yield|silence on a granted grab")
        out["reason"] = value["reason"]
    return out


def _v_unit(value: dict[str, Any], where: str) -> dict[str, Any]:
    _only(value, {"op", "unit"}, where)
    return {"op": value["op"], "unit": unit_field(value, where)}


def _v_onair(value: dict[str, Any], where: str) -> dict[str, Any]:
    _only(value, {"op", "side"}, where)
    return {"op": "onair", "side": side_field(value, "side", where)}


def _v_setting(value: dict[str, Any], where: str) -> dict[str, Any]:
    _only(value, {"op", "side", *SETTING_KEYS}, where)
    out: dict[str, Any] = {"op": "setting", "side": side_field(value, "side", where)}
    if "lock_half_on_turn" in value:
        out["lock_half_on_turn"] = _bool(value, "lock_half_on_turn", where)
    if "audio_laptop" in value:
        out["audio_laptop"] = side_field(value, "audio_laptop", where)
    if "two_outputs" in value:
        out["two_outputs"] = _bool(value, "two_outputs", where)
    if len(out) == 2:
        raise BoothError("bad_field", f"{where} changes nothing: give lock_half_on_turn, audio_laptop or two_outputs")
    return out


def _v_end(value: dict[str, Any], where: str) -> dict[str, Any]:
    _only(value, {"op"}, where)
    return {"op": "end"}


def _v_ready(value: dict[str, Any], where: str) -> dict[str, Any]:
    """B2BBOOTH-124: the audio laptop's page reports a deck's committed load (``seq``, 0 for none) loaded there,
    and where that deck sits (its cue point)."""
    _only(value, {"op", "deck", "seq", "pos_frames"}, where)
    return {"op": "ready", "deck": deck_field(value, where), "seq": _int(value, "seq", 0, where),
            "pos_frames": _num(value, "pos_frames", where)}


_VALIDATORS: dict[str, Any] = {
    "load": _v_load,
    "ready": _v_ready,
    **dict.fromkeys(POSITIONED_OPS, _v_positioned),
    "loop_off": _v_loop_off,
    "grab": _v_grab,
    **dict.fromkeys(("yield", "refuse", "release", "locked"), _v_unit),
    "onair": _v_onair,
    "setting": _v_setting,
    "end": _v_end,
}


def validate_op(value: Any) -> dict[str, Any]:
    """One discrete op as stored, or ``BoothError`` (unknown op names and keys refused)."""
    if not isinstance(value, dict):
        raise BoothError("bad_op", "an op must be a JSON object")
    validator = _VALIDATORS.get(value.get("op"))  # type: ignore[arg-type]
    if validator is None:
        raise BoothError("bad_op", f"unknown op {value.get('op')!r}")
    return validator(value, f"op {value.get('op')!r}")


def validate_committed(value: Any) -> dict[str, Any]:
    value = _obj(value, "committed")
    _only(value, {"seq", "by", "op", "pid"}, "committed")
    pid = value.get("pid")
    if pid is not None:
        pid = _int(value, "pid", 1, "committed")
    return {
        "seq": _int(value, "seq", 1, "committed"),
        "by": side_field(value, "by", "committed"),
        "op": validate_op(value.get("op")),
        "pid": pid,
    }


def validate_ctl_name(ctl: Any) -> str:
    if ctl in LOCAL_ONLY_CTLS:
        raise BoothError("local_only", f"{ctl} is this laptop's own output and never travels")
    if not isinstance(ctl, str) or not CTL_NAME_RE.fullmatch(ctl):
        raise BoothError("bad_field", f"ctl {ctl!r} is not a booth control")
    return ctl


def validate_trace(value: Any) -> dict[str, int]:
    """Latency stamps riding a ctl or propose (each in the SENDER's Link clock)."""
    value = _obj(value, "trace")
    _only(value, TRACE_KEYS, "trace")
    return {k: _int(value, k, 0, "trace") for k in value}


# ---------------------------------------------------------------- engine-to-engine


def _committed_list(value: Any, where: str, *, contiguous: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise BoothError("bad_field", f"{where}.ops must be a list")
    ops = [validate_committed(c) for c in value]
    if contiguous:
        for i, op in enumerate(ops):
            if op["seq"] != i + 1:
                raise BoothError("bad_field", f"{where}.ops[{i}].seq must be {i + 1}, got {op['seq']}")
    return ops


def _v_small_message(kind: str, value: dict[str, Any]) -> dict[str, Any]:
    """The ctl and dgram lanes' small kinds: ``renew``, ``udp`` (door only), ``ping`` / ``pong`` (dgram only)."""
    if kind == "udp":
        _only(value, {"kind", "port", "sid"}, "udp")
        port = _int(value, "port", 1, "udp")
        sid = _str(value, "sid", "udp")
        if port > 65535 or not SID_RE.fullmatch(sid):
            raise BoothError("bad_field", f"udp needs a port 1..65535 and a 16-hex sid, got {port!r} {sid!r}")
        return {"kind": kind, "port": port, "sid": sid}
    if kind == "ping":
        _only(value, {"kind", "t0_us"}, "ping")
        return {"kind": kind, "t0_us": _int(value, "t0_us", 0, "ping")}
    if kind == "pong":
        _only(value, {"kind", "t0_us", "now_us"}, "pong")
        return {"kind": kind, "t0_us": _int(value, "t0_us", 0, "pong"), "now_us": _int(value, "now_us", 0, "pong")}
    _only(value, {"kind", "side", "units", "at_link_us"}, "renew")
    units = value.get("units")
    if not isinstance(units, list) or any(u not in UNITS for u in units):
        raise BoothError("bad_field", f"renew.units must list booth units, got {units!r}")
    return {"kind": kind, "side": side_field(value, "side", "renew"), "units": list(units),
            "at_link_us": _int(value, "at_link_us", 0, "renew")}


def validate_message(value: Any, lane: Lane) -> dict[str, Any]:
    """One message inside an envelope, for ``lane``; a kind on the wrong lane is refused."""
    value = _obj(value, "message")
    kind = value.get("kind")
    allowed = _KINDS_BY_LANE[lane]
    if kind not in allowed:
        raise BoothError("bad_kind", f"kind {kind!r} does not travel on the {lane} lane")
    if kind == "propose":
        _only(value, {"kind", "from", "pid", "op", "trace"}, "propose")
        out = {"kind": kind, "from": side_field(value, "from", "propose"), "pid": _int(value, "pid", 1, "propose"),
               "op": validate_op(value.get("op"))}
    elif kind == "commit":
        _only(value, {"kind", "ops", "traces"}, "commit")
        out = {"kind": kind, "ops": _committed_list(value.get("ops"), "commit", contiguous=False)}
        traces: dict[str, dict[str, int]] = {}
        for key, trace in _obj(value.get("traces", {}), "commit.traces").items():
            if not (key.isdigit() and int(key) >= 1):
                raise BoothError("bad_field", f"commit.traces keys are seq numbers, got {key!r}")
            traces[key] = validate_trace(trace)
        out["traces"] = traces
    elif kind == "resend":
        _only(value, {"kind", "from"}, "resend")
        out = {"kind": kind, "from": _int(value, "from", 1, "resend")}
    elif kind == "snapshot":
        _only(value, {"kind", "ops"}, "snapshot")
        out = {"kind": kind, "ops": _committed_list(value.get("ops"), "snapshot", contiguous=True)}
    elif kind == "ctl":
        _only(value, {"kind", "side", "unit", "ctl", "value", "cseq", "at_link_us", "trace"}, "ctl")
        out = {"kind": kind, "side": side_field(value, "side", "ctl"), "unit": unit_field(value, "ctl"),
               "ctl": validate_ctl_name(value.get("ctl")), "value": _num(value, "value", "ctl"),
               "cseq": _int(value, "cseq", 1, "ctl"), "at_link_us": _int(value, "at_link_us", 0, "ctl")}
    elif kind == "pos":
        _only(value, {"kind", "deck", "pos_frames", "at_link_us", "rate"}, "pos")
        out = {"kind": kind, "deck": deck_field(value, "pos"), "pos_frames": _num(value, "pos_frames", "pos"),
               "at_link_us": _int(value, "at_link_us", 0, "pos"), "rate": _num(value, "rate", "pos")}
    else:  # renew, udp, ping, pong
        out = _v_small_message(kind, value)
    if "trace" in value and kind in ("propose", "ctl"):
        out["trace"] = validate_trace(value["trace"])
    return out


def validate_envelope(value: Any, lane: Lane) -> dict[str, Any]:
    """One door POST body from the partner's engine."""
    value = _obj(value, "envelope")
    _only(value, {"v", "epoch", "sent_us", "messages"}, "envelope")
    if value.get("v") != PROTOCOL_VERSION:
        raise BoothError("bad_version", f"v must be {PROTOCOL_VERSION}, got {value.get('v')!r}")
    messages = value.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BoothError("bad_field", "envelope.messages must be a non-empty list")
    shaped = [validate_message(m, lane) for m in messages]
    epoch = epoch_field(value.get("epoch"), nullable=True)
    if epoch is None and any(m["kind"] != "resend" for m in shaped):
        raise BoothError("bad_epoch", "only a joiner's first resend may travel without an epoch")
    return {"v": PROTOCOL_VERSION, "epoch": epoch, "sent_us": _int(value, "sent_us", 0, "envelope"), "messages": shaped}


# ---------------------------------------------------------------- intents from this laptop's page


def validate_intent(value: Any) -> dict[str, Any]:
    """``POST /booth/intent``: ``{kind: op, op}`` or ``{kind: control, unit, ctl, value}``, each with an
    optional ``t_page_us`` (the page's Link time at input, for the latency trace)."""
    value = _obj(value, "intent")
    kind = value.get("kind")
    if kind == "op":
        _only(value, {"kind", "op", "t_page_us"}, "intent")
        out = {"kind": kind, "op": validate_op(value.get("op"))}
    elif kind == "control":
        _only(value, {"kind", "unit", "ctl", "value", "t_page_us"}, "intent")
        out = {"kind": kind, "unit": unit_field(value, "intent"), "ctl": validate_ctl_name(value.get("ctl")),
               "value": _num(value, "value", "intent")}
    else:
        raise BoothError("bad_intent", f"intent kind must be op or control, got {kind!r}")
    if "t_page_us" in value:
        out["t_page_us"] = _int(value, "t_page_us", 0, "intent")
    return out


def validate_position(value: Any) -> dict[str, Any]:
    """``POST /booth/position``: the audio laptop page's sample for one deck."""
    value = _obj(value, "position")
    _only(value, {"deck", "pos_frames", "at_link_us", "rate"}, "position")
    return {"deck": deck_field(value, "position"), "pos_frames": _num(value, "pos_frames", "position"),
            "at_link_us": _int(value, "at_link_us", 0, "position"), "rate": _num(value, "rate", "position")}


def validate_invite(body: Any) -> tuple[Side, Side]:
    """B2BBOOTH-121: ``{v: 1, kind: "invite", from, audio_laptop}`` -> ``(from, audio_laptop)``, or ``BoothError``."""
    if not isinstance(body, dict):
        raise BoothError("bad_invite", "an invite is a JSON object")
    _only(body, {"v", "kind", "from", "audio_laptop"}, "invite")
    if body.get("v") != PROTOCOL_VERSION or body.get("kind") != "invite":
        raise BoothError("bad_invite", f"an invite is {{v: {PROTOCOL_VERSION}, kind: invite, from, audio_laptop}}")
    return side_field(body, "from", "invite"), side_field(body, "audio_laptop", "invite")
