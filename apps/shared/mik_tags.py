"""Mixed In Key's own ID3 frames: the key and tempo MIK wrote into the file (KEY-FALLBACK-01).

MIK does not write TKEY/TBPM on these files. It writes ID3 ``GEOB`` frames whose
payload is base64 JSON: description ``Key`` -> ``{"key": "9A", "source":
"mixedinkey", ...}`` and ``BeatGrid`` -> ``{"tempo": 123.99, "beats": [...],
"source": "mixedinkey"}`` (measured on 26 of 28 present files of "Viva - The cut",
Wed 4 Nov 2026). Only the ID3 header is read, never the audio.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 KEY-FALLBACK-01 read MIK's key and tempo from the file
    [if] a file carries GEOB Key / BeatGrid from mixedinkey [then] key and bpm come back
    [if] a file has no ID3 tag or no MIK frames [then] both are None, never a guess
    [if] a MIK frame holds a key that is not a Camelot key [then] it is ignored

-Claude
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared import id3v2

#: A Camelot key as MIK writes it: 1A..12B.
_CAMELOT = re.compile(r"^(1[0-2]|[1-9])[AB]$")
#: Plausible DJ tempo; MIK's BeatGrid tempo outside it is not a tempo.
_BPM_RANGE: tuple[float, float] = (40.0, 300.0)


@dataclass(frozen=True)
class MikTagValues:
    key: str | None
    bpm: float | None


def _geob_json(payload: bytes) -> dict[str, Any] | None:
    try:
        decoded = json.loads(base64.b64decode(payload))
    except (binascii.Error, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def values_from_geob(frames: list[tuple[str, str, str, bytes]]) -> MikTagValues:
    """MIK key + tempo from GEOB ``(mime, filename, description, payload)`` frames."""
    key: str | None = None
    bpm: float | None = None
    for _mime, _name, description, payload in frames:
        body = _geob_json(payload)
        if body is None or body.get("source") != "mixedinkey":
            continue
        if description == "Key" and isinstance(body.get("key"), str) and _CAMELOT.match(body["key"]):
            key = body["key"]
        elif description == "BeatGrid" and isinstance(body.get("tempo"), (int, float)):
            tempo = round(float(body["tempo"]), 2)
            if _BPM_RANGE[0] <= tempo <= _BPM_RANGE[1]:
                bpm = tempo
    return MikTagValues(key=key, bpm=bpm)


def read_mik_values(path: Path) -> MikTagValues:
    """MIK's key and tempo from ``path``'s ID3 tag; both None when it has none."""
    tag = id3v2.read_tag(path)
    if tag is None:
        return MikTagValues(key=None, bpm=None)
    return values_from_geob(tag.geob())
