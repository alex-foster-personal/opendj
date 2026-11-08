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
  ✔︎ ✅ 🎯 mik_geob_json() is the one MIK GEOB reader; apps.analysis_beatgrid.mik_grid reads its
    BeatGrid through it (issue #8744)
    [if] a frame's payload is not base64 JSON of an object, or its source is not mixedinkey [then] None

-Claude
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Sequence
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


GeobFrame = tuple[str, str, str, bytes]


def mik_geob_json(frames: Sequence[GeobFrame], description: str) -> dict[str, Any] | None:
    """The JSON body of the first GEOB frame named ``description``, when Mixed In Key wrote it.

    The one MIK GEOB parser: frames are ``(mime, filename, description, payload)`` and the payload is
    base64 JSON. None when no frame has that description, its payload is not a JSON object, or its
    ``source`` is not ``mixedinkey``.
    """
    payload = next((frame[3] for frame in frames if frame[2] == description), None)
    if payload is None:
        return None
    try:
        body = json.loads(base64.b64decode(payload))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(body, dict) or body.get("source") != "mixedinkey":
        return None
    return body


def values_from_geob(frames: Sequence[GeobFrame]) -> MikTagValues:
    """MIK key + tempo from GEOB ``(mime, filename, description, payload)`` frames."""
    key_body = mik_geob_json(frames, "Key") or {}
    grid_body = mik_geob_json(frames, "BeatGrid") or {}
    raw_key, raw_tempo = key_body.get("key"), grid_body.get("tempo")
    key = raw_key if isinstance(raw_key, str) and _CAMELOT.match(raw_key) else None
    bpm: float | None = None
    if isinstance(raw_tempo, (int, float)) and not isinstance(raw_tempo, bool):
        tempo = round(float(raw_tempo), 2)
        bpm = tempo if _BPM_RANGE[0] <= tempo <= _BPM_RANGE[1] else None
    return MikTagValues(key=key, bpm=bpm)


def read_mik_values(path: Path) -> MikTagValues:
    """MIK's key and tempo from ``path``'s ID3 tag; both None when it has none."""
    tag = id3v2.read_tag(path)
    if tag is None:
        return MikTagValues(key=None, bpm=None)
    return values_from_geob(tag.geob())
