"""OS2L message codec (LIGHT-10 in the lighting spec): beat, btn and cmd out, feedback in.

OS2L (os2l.org) is JSON over TCP from the audio app (client) to the lighting app (server).
Each message is one JSON object whose ``evt`` names its kind. The protocol defines no
delimiter between objects, so the decoder splits a byte stream on complete top-level objects.
Pure: no sockets, no discovery (those are separate lighting issues). Built from the public
os2l.org page only.
"""

from __future__ import annotations

import codecs
import json
import math
from dataclasses import dataclass
from typing import Literal

ButtonState = Literal["on", "off"]
_BUTTON_STATES: tuple[ButtonState, ...] = ("on", "off")


# ----------------------------------------------------------------------------- messages


@dataclass(frozen=True)
class Beat:
    """A beat: ``pos`` counts beats since playback start, ``change`` marks a tempo or position jump."""

    pos: int
    bpm: float
    change: bool = False
    strength: int | None = None


@dataclass(frozen=True)
class Btn:
    """A virtual button press or release on the lighting app."""

    name: str
    state: ButtonState
    page: str | None = None


@dataclass(frozen=True)
class Cmd:
    """A numbered command with a 0..100 parameter."""

    id: int
    param: float


@dataclass(frozen=True)
class Feedback:
    """The lighting app reporting a button's state back to the audio app."""

    name: str
    page: str | None
    state: ButtonState


OutMessage = Beat | Btn | Cmd


# ----------------------------------------------------------------------------- validation helpers


def _require_int(field: str, value: object, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"os2l {field} must be an int, got {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"os2l {field} must be >= {minimum}, got {value}")
    return value


def _require_range(field: str, value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"os2l {field} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"os2l {field} must be finite, got {value!r}")
    if not low <= value <= high:
        raise ValueError(f"os2l {field} must be in {low:g}..{high:g}, got {value}")
    return float(value)


def _require_text(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"os2l {field} must be a string, got {value!r}")
    if not value:
        raise ValueError(f"os2l {field} must not be empty")
    return value


def _require_state(value: object) -> ButtonState:
    if value not in _BUTTON_STATES:
        raise ValueError(f"os2l state must be 'on' or 'off', got {value!r}")
    return value  # type: ignore[return-value]


# ----------------------------------------------------------------------------- encode


def _beat_payload(msg: Beat) -> dict[str, object]:
    if not isinstance(msg.change, bool):
        raise TypeError(f"os2l change must be a bool, got {msg.change!r}")
    bpm = _require_range("bpm", msg.bpm, 0.0, math.inf)
    if bpm <= 0:
        raise ValueError(f"os2l bpm must be > 0, got {msg.bpm}")
    payload: dict[str, object] = {
        "evt": "beat",
        "change": msg.change,
        "pos": _require_int("pos", msg.pos, minimum=0),
        "bpm": bpm,
    }
    if msg.strength is not None:
        _require_range("strength", msg.strength, 0, 100)
        payload["strength"] = _require_int("strength", msg.strength)
    return payload


def _btn_payload(msg: Btn) -> dict[str, object]:
    payload: dict[str, object] = {"evt": "btn", "name": _require_text("name", msg.name)}
    if msg.page is not None:
        payload["page"] = _require_text("page", msg.page)
    payload["state"] = _require_state(msg.state)
    return payload


def _cmd_payload(msg: Cmd) -> dict[str, object]:
    return {
        "evt": "cmd",
        "id": _require_int("id", msg.id, minimum=0),
        "param": _require_range("param", msg.param, 0, 100),
    }


def encode(msg: OutMessage) -> bytes:
    """One outgoing message as compact UTF-8 JSON. Raises ValueError on any out-of-range field."""
    if isinstance(msg, Beat):
        payload = _beat_payload(msg)
    elif isinstance(msg, Btn):
        payload = _btn_payload(msg)
    elif isinstance(msg, Cmd):
        payload = _cmd_payload(msg)
    else:
        raise TypeError(f"os2l cannot encode {type(msg).__name__}")
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


# ----------------------------------------------------------------------------- decode


def _feedback_from(obj: object) -> Feedback:
    if not isinstance(obj, dict):
        raise TypeError(f"os2l message must be a JSON object, got {type(obj).__name__}")
    evt = obj.get("evt")
    if evt != "feedback":
        raise ValueError(f"os2l unknown inbound evt {evt!r}")
    unknown = set(obj) - {"evt", "name", "page", "state"}
    if unknown:
        raise ValueError(f"os2l feedback has unknown fields {sorted(unknown)}")
    page = obj.get("page")
    return Feedback(
        name=_require_text("name", obj.get("name")),
        page=None if page is None else _require_text("page", page),
        state=_require_state(obj.get("state")),
    )


class Os2lDecoder:
    """Incremental decoder for the inbound stream: feed bytes as they arrive, get whole messages.

    Objects may arrive several per chunk or split across chunks (including inside a multi-byte
    UTF-8 character). Anything that is not a JSON object, or an object with an evt other than
    ``feedback``, raises ValueError naming it; nothing is dropped silently.
    """

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")()
        self._pending = ""

    def feed(self, chunk: bytes) -> list[Feedback]:
        self._pending += self._utf8.decode(chunk)
        messages: list[Feedback] = []
        while (end := self._complete_object_end()) is not None:
            text, self._pending = self._pending[:end], self._pending[end:]
            messages.append(_feedback_from(json.loads(text)))
        return messages

    def _complete_object_end(self) -> int | None:
        """Index just past the first complete top-level object, or None while it is incomplete."""
        stripped = self._pending.lstrip()
        self._pending = stripped
        if not stripped:
            return None
        if stripped[0] != "{":
            raise ValueError(f"os2l stream expected '{{', got {stripped[:20]!r}")
        depth = 0
        in_string = False
        escaped = False
        for index, char in enumerate(stripped):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index + 1
        return None
