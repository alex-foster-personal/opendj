"""B2BBOOTH-01, B2BBOOTH-12, B2BBOOTH-90: the engine's wire validation (ported from the page's
``b2b-booth-protocol.test.mjs``; S1b moved the parser into the engine).

- [if] an envelope carries a version other than 1 [then] it is refused bad_version
- [if] a message has an unknown kind, a kind on the wrong lane, or an unknown op [then] it is refused, never returned
- [if] a message carries a key the contract does not name [then] it is refused extra_keys
- [if] every message kind is well formed [then] it validates to exactly its fields
- [if] loop_off or the trim control is refused [then] the wave-1 contract gaps are still open
- [if] the engine's validation breaks any line above [then] fail, [else stop].
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.b2b_booth.protocol import (
    BoothError,
    half_of_unit,
    units_of_half,
    validate_envelope,
    validate_intent,
    validate_position,
)

pytestmark = [pytest.mark.requirement(r) for r in ("B2BBOOTH-01", "B2BBOOTH-12", "B2BBOOTH-90")]

EPOCH = "0123456789abcdef"
TRACK = {"sha256": "a" * 64, "bytes": 1000, "title": "t", "artist": "a", "bpm": 124.0, "key": "8A", "duration_s": 300.0,
         "owner_track_id": "x1"}


def _env(*messages: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"v": 1, "epoch": EPOCH, "sent_us": 5, "messages": list(messages), **extra}


GOOD_OPS = [
    {"kind": "propose", "from": "joiner", "pid": 1, "op": {"op": "play", "deck": 3, "at_link_us": 10, "pos_frames": 0}},
    {"kind": "propose", "from": "joiner", "pid": 2, "op": {"op": "load", "deck": 1, "track": TRACK}},
    {"kind": "propose", "from": "inviter", "pid": 3, "op": {"op": "loop", "deck": 2, "at_link_us": 1, "pos_frames": 5, "loop": {"in": 1, "out": 2}}},
    {"kind": "propose", "from": "joiner", "pid": 4, "op": {"op": "loop_off", "deck": 4}},
    {"kind": "propose", "from": "inviter", "pid": 5, "op": {"op": "setting", "side": "inviter", "audio_laptop": "joiner"},
     "trace": {"page_us": 1, "engine_us": 2}},
    {"kind": "commit", "ops": [{"seq": 1, "by": "joiner", "pid": 1, "op": {"op": "grab", "unit": "deck:1", "phase": "granted", "reason": "silence"}}],
     "traces": {"1": {"page_us": 1, "engine_us": 2, "inviter_us": 3}}},
    {"kind": "commit", "ops": [{"seq": 2, "by": "inviter", "pid": None, "op": {"op": "onair", "side": "joiner"}}], "traces": {}},
    {"kind": "resend", "from": 4},
    {"kind": "snapshot", "ops": [{"seq": 1, "by": "inviter", "pid": None, "op": {"op": "release", "unit": "xfader"}}]},
]
GOOD_CTL = [
    {"kind": "ctl", "side": "joiner", "unit": "strip:3", "ctl": "eq.low", "value": 0.5, "cseq": 9, "at_link_us": 100},
    {"kind": "ctl", "side": "joiner", "unit": "strip:3", "ctl": "trim", "value": 0.8, "cseq": 1, "at_link_us": 100},
    {"kind": "ctl", "side": "joiner", "unit": "deck:3", "ctl": "fx.2.amount", "value": 0.5, "cseq": 1, "at_link_us": 100, "trace": {"page_us": 7}},
    {"kind": "pos", "deck": 4, "pos_frames": 4410.5, "at_link_us": 100, "rate": 1.02},
    {"kind": "renew", "side": "joiner", "units": ["deck:3", "strip:3"], "at_link_us": 5},
]


@pytest.mark.parametrize("message", GOOD_OPS, ids=lambda m: f"{m['kind']}-{m.get('op', {}).get('op', '')}")
def test_every_ops_kind_validates_to_exactly_its_fields(message: dict[str, Any]) -> None:
    assert validate_envelope(_env(message), "ops")["messages"] == [message]


@pytest.mark.parametrize("message", GOOD_CTL, ids=lambda m: f"{m['kind']}-{m.get('ctl', '')}")
def test_every_ctl_kind_validates_to_exactly_its_fields(message: dict[str, Any]) -> None:
    assert validate_envelope(_env(message), "ctl")["messages"] == [message]


BAD: list[tuple[str, str, dict[str, Any]]] = [
    ("bad_version", "ops", _env({"kind": "resend", "from": 1}, v=2)),
    ("bad_version", "ops", {"epoch": EPOCH, "sent_us": 1, "messages": [{"kind": "resend", "from": 1}]}),
    ("bad_kind", "ops", _env({"kind": "hello"})),
    ("bad_kind", "ctl", _env({"kind": "resend", "from": 1})),
    ("bad_kind", "ops", _env({"kind": "ctl", "side": "joiner", "unit": "strip:3", "ctl": "fader", "value": 1, "cseq": 1, "at_link_us": 0})),
    ("bad_epoch", "ops", {"v": 1, "epoch": "XYZ", "sent_us": 1, "messages": [{"kind": "resend", "from": 1}]}),
    ("bad_epoch", "ops", {"v": 1, "epoch": None, "sent_us": 1, "messages": [GOOD_OPS[0]]}),
    ("bad_op", "ops", _env({"kind": "propose", "from": "joiner", "pid": 1, "op": {"op": "scratch", "deck": 1}})),
    ("extra_keys", "ops", _env({"kind": "resend", "from": 1, "extra": True})),
    ("extra_keys", "ops", _env({"kind": "propose", "from": "joiner", "pid": 1, "op": {"op": "end", "why": "x"}})),
    ("extra_keys", "ops", _env({"kind": "propose", "from": "joiner", "pid": 1, "op": {"op": "loop_off", "deck": 1, "at_link_us": 1}})),
    ("extra_keys", "ops", _env({"kind": "resend", "from": 1}, extra=1)),
    ("bad_field", "ops", _env({"kind": "propose", "from": "joiner", "pid": 1, "op": {"op": "play", "deck": 5, "at_link_us": 0, "pos_frames": 0}})),
    ("bad_field", "ops", _env({"kind": "propose", "from": "joiner", "pid": 1, "op": {"op": "grab", "unit": "deck:9", "phase": "ask"}})),
    ("bad_field", "ops", _env({"kind": "propose", "from": "joiner", "pid": 1,
                               "op": {"op": "grab", "unit": "deck:1", "phase": "ask", "reason": "yield"}})),
    ("bad_field", "ops", _env({"kind": "propose", "from": "joiner", "pid": 1, "op": {"op": "setting", "side": "joiner"}})),
    ("bad_field", "ops", _env({"kind": "snapshot", "ops": [{"seq": 2, "by": "inviter", "pid": None, "op": {"op": "end"}}]})),
    ("bad_field", "ops", _env({"kind": "commit", "ops": [], "traces": {"x": {}}})),
    ("bad_field", "ops", _env()),
    ("local_only", "ctl", _env({"kind": "ctl", "side": "joiner", "unit": "strip:3", "ctl": "cue_bus", "value": 1, "cseq": 1, "at_link_us": 0})),
    ("bad_field", "ctl", _env({"kind": "ctl", "side": "joiner", "unit": "strip:3", "ctl": "eq.loud", "value": 1, "cseq": 1, "at_link_us": 0})),
    ("bad_field", "ctl", _env({"kind": "renew", "side": "joiner", "units": ["deck:9"], "at_link_us": 0})),
    ("bad_field", "ops", []),  # not an object
]


@pytest.mark.parametrize(("code", "lane", "value"), BAD, ids=[f"{c}-{i}" for i, (c, _, _) in enumerate(BAD)])
def test_bad_messages_are_refused_with_a_named_code(code: str, lane: str, value: Any) -> None:
    with pytest.raises(BoothError) as err:
        validate_envelope(value, lane)  # type: ignore[arg-type]
    assert err.value.code == code


def test_only_a_joiners_first_resend_travels_without_an_epoch() -> None:
    shaped = validate_envelope({"v": 1, "epoch": None, "sent_us": 1, "messages": [{"kind": "resend", "from": 1}]}, "ops")
    assert shaped["epoch"] is None


def test_intents_and_positions() -> None:
    assert validate_intent({"kind": "op", "op": {"op": "loop_off", "deck": 2}, "t_page_us": 9}) == {
        "kind": "op", "op": {"op": "loop_off", "deck": 2}, "t_page_us": 9}
    assert validate_intent({"kind": "control", "unit": "strip:2", "ctl": "trim", "value": 1}) == {
        "kind": "control", "unit": "strip:2", "ctl": "trim", "value": 1}
    for bad in ({"kind": "control", "unit": "strip:2", "ctl": "trim"}, {"kind": "op"}, {"kind": "op", "op": {"op": "end"}, "t_page_us": -1}):
        with pytest.raises(BoothError):
            validate_intent(bad)
    assert validate_position({"deck": 1, "pos_frames": 1.5, "at_link_us": 3, "rate": 0.0})["rate"] == 0.0
    with pytest.raises(BoothError):
        validate_position({"deck": 1, "pos_frames": float("nan"), "at_link_us": 3, "rate": 1.0})


def test_halves() -> None:
    assert units_of_half("inviter") == ["deck:1", "strip:1", "deck:2", "strip:2"]
    assert units_of_half("joiner") == ["deck:3", "strip:3", "deck:4", "strip:4"]
    assert (half_of_unit("strip:2"), half_of_unit("deck:3"), half_of_unit("xfader"), half_of_unit("master")) == (
        "inviter", "joiner", None, None)
