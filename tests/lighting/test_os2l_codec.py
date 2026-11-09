"""LIGHT-9955: OS2L message codec, beat, btn and cmd out and feedback in.

[if] a beat with pos 42 and bpm 120 does not encode to {"evt":"beat","change":false,"pos":42,"bpm":120.0} [then] fail, [else stop].
[if] a strength of 130 (or any field out of range) encodes [then] fail, [else stop].
[if] two objects in one chunk, or one object over three chunks, do not decode exactly and in order [then] fail, [else stop].
[if] an object with an unknown evt is dropped instead of raising with the evt name [then] fail, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.lighting.os2l_codec import Beat, Btn, Cmd, Feedback, Os2lDecoder, encode

pytestmark = [pytest.mark.requirement("LIGHT-9955")]


def _feedback_bytes(name: str, page: str, state: str) -> bytes:
    return json.dumps({"evt": "feedback", "name": name, "page": page, "state": state}).encode()


# ----------------------------------------------------------------------------- encode


def test_beat_encodes_to_the_os2l_beat_object() -> None:
    """[if] a beat with pos 42 and bpm 120 is encoded [then] the bytes parse to the beat object."""
    assert json.loads(encode(Beat(pos=42, bpm=120))) == {"evt": "beat", "change": False, "pos": 42, "bpm": 120.0}
    raw = encode(Beat(pos=42, bpm=120))
    assert isinstance(json.loads(raw)["bpm"], float)
    assert json.loads(encode(Beat(pos=0, bpm=128.5, change=True, strength=100))) == {
        "evt": "beat",
        "change": True,
        "pos": 0,
        "bpm": 128.5,
        "strength": 100,
    }


def test_btn_and_cmd_encode() -> None:
    """[if] btn or cmd lose a field or add an absent optional page [then] fail."""
    assert json.loads(encode(Btn(name="Strobe", state="on", page="FX"))) == {
        "evt": "btn",
        "name": "Strobe",
        "page": "FX",
        "state": "on",
    }
    assert json.loads(encode(Btn(name="Strobe", state="off"))) == {"evt": "btn", "name": "Strobe", "state": "off"}
    assert json.loads(encode(Cmd(id=3, param=55))) == {"evt": "cmd", "id": 3, "param": 55.0}


@pytest.mark.parametrize(
    ("message", "field"),
    [
        (Beat(pos=1, bpm=120, strength=130), "strength"),
        (Beat(pos=1, bpm=120, strength=-1), "strength"),
        (Beat(pos=-1, bpm=120), "pos"),
        (Beat(pos=1, bpm=0), "bpm"),
        (Beat(pos=1, bpm=float("nan")), "bpm"),
        (Btn(name="", state="on"), "name"),
        (Btn(name="Strobe", state="maybe"), "state"),  # type: ignore[arg-type]
        (Cmd(id=1, param=100.5), "param"),
        (Cmd(id=-2, param=10), "id"),
    ],
)
def test_out_of_range_fields_raise_value_error(message: Beat | Btn | Cmd, field: str) -> None:
    """[if] strength is 130 (or any field is out of range) [then] encoding raises ValueError naming it."""
    with pytest.raises(ValueError, match=field):
        encode(message)


# ----------------------------------------------------------------------------- decode


def test_two_objects_in_one_chunk_decode_in_order() -> None:
    """[if] two feedback objects arrive in one chunk [then] the decoder yields both, in order."""
    decoder = Os2lDecoder()
    chunk = _feedback_bytes("Strobe", "FX", "on") + _feedback_bytes("Blackout", "Main", "off")
    assert decoder.feed(chunk) == [
        Feedback(name="Strobe", page="FX", state="on"),
        Feedback(name="Blackout", page="Main", state="off"),
    ]
    assert decoder.feed(b"") == []


def test_one_object_split_over_three_chunks_decodes_once() -> None:
    """[if] one object is split across three chunks [then] the decoder yields exactly that object."""
    decoder = Os2lDecoder()
    raw = json.dumps({"evt": "feedback", "name": 'Fog {big} "x"', "page": "Café", "state": "on"}).encode()
    raw = raw.replace(b"\\u00e9", "é".encode())  # a real two-byte character to split inside
    cut_inside_char = raw.index("é".encode()) + 1
    parts = [raw[:7], raw[7:cut_inside_char], raw[cut_inside_char:]]
    assert decoder.feed(parts[0]) == []
    assert decoder.feed(parts[1]) == []
    assert decoder.feed(parts[2]) == [Feedback(name='Fog {big} "x"', page="Café", state="on")]


def test_stream_with_whitespace_and_trailing_partial_keeps_order() -> None:
    """[if] objects straddle chunks with whitespace between [then] every object comes out once, in order."""
    decoder = Os2lDecoder()
    stream = b"\n".join(_feedback_bytes(f"b{i}", "P", "on" if i % 2 else "off") for i in range(5))
    out: list[Feedback] = []
    for start in range(0, len(stream), 11):
        out.extend(decoder.feed(stream[start : start + 11]))
    assert [f.name for f in out] == [f"b{i}" for i in range(5)]


def test_unknown_evt_raises_with_its_name() -> None:
    """[if] an object with an unknown evt arrives [then] the decoder raises with the evt name."""
    decoder = Os2lDecoder()
    with pytest.raises(ValueError, match="'telemetry'"):
        decoder.feed(b'{"evt":"telemetry","x":1}')


def test_garbage_and_bad_feedback_raise() -> None:
    """[if] non-object bytes or a feedback with a bad state arrive [then] the decoder raises, never drops."""
    with pytest.raises(ValueError, match="expected"):
        Os2lDecoder().feed(b"hello")
    with pytest.raises(ValueError, match="state"):
        Os2lDecoder().feed(b'{"evt":"feedback","name":"a","page":"b","state":"half"}')


def test_no_u2014_or_u2013_in_the_new_files() -> None:
    """[if] a new file carries U+2014 or U+2013 [then] fail."""
    root = Path(__file__).resolve().parents[2]
    for rel in ("apps/lighting/os2l_codec.py", "tests/lighting/test_os2l_codec.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text, rel
