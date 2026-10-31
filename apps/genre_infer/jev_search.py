"""Cmd K AI search (CMDK-20, issue #6093): rank a shortlist by JEV, in ONE call.

The bar shortlists tracks that can mix into the master (BPM range, compatible
key, not already played) from its own in-memory index, then sends that list
here with the DJ's words. JEV (``~typesafe/jev-latest`` on the OpenRouter
decisions endpoint, :mod:`apps.genre_infer.jev`) gets one STATE listing every
shortlisted track by number and one yes/no ("noul") question per track,
"does track N fit: <the words>?", and answers a probability for each. JEV
charges per call, not per question, so a whole shortlist costs one call.

The model sees only the text the library already has (artist, title, album,
genre, BPM, key); it never hears audio, and nothing it answers is written
anywhere. A missing key, a failed call or an answer missing any track is an
error with a reason, never a partial or invented ranking.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apps.genre_infer import jev

#: The bar shortlists about 50; more would crowd JEV's context with little gain.
MAX_TRACKS = 60
MAX_WORDS = 200

_FIELDS: tuple[tuple[str, str], ...] = (
    ("artist", "Artist"),
    ("title", "Title"),
    ("album", "Album"),
    ("genre", "Genre"),
    ("bpm", "BPM"),
    ("key", "Key"),
)


class SearchKeyMissing(RuntimeError):
    """No OpenRouter key in the engine's environment."""


class SearchFailed(RuntimeError):
    """The call failed or its answer was unusable; the message says which."""


@dataclass(frozen=True)
class SearchTrack:
    stable_id: str
    fields: Mapping[str, Any]


def track_line(index: int, fields: Mapping[str, Any]) -> str:
    """``Track N: Artist: x; Title: y; BPM: 124`` with empty fields left out."""
    parts = []
    for key, label in _FIELDS:
        value = fields.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, float):
            value = f"{value:g}"
        parts.append(f"{label}: {value}")
    return f"Track {index}: " + ("; ".join(parts) if parts else "(nothing known)")


def build_request(words: str, tracks: Sequence[SearchTrack], model: str) -> dict[str, Any]:
    """One STATE with every track numbered from 1, one noul question per track."""
    words = words.strip()
    if not words:
        raise ValueError("AI search needs words to judge the tracks against")
    if len(words) > MAX_WORDS:
        raise ValueError(f"AI search words are limited to {MAX_WORDS} characters")
    if not tracks:
        raise ValueError("AI search needs at least one track")
    if len(tracks) > MAX_TRACKS:
        raise ValueError(f"AI search takes at most {MAX_TRACKS} tracks, got {len(tracks)}")
    lines = [
        "The STATE lists tracks from a DJ's music library by number, with whatever artist, "
        "title, album, genre, tempo and key the library knows. The DJ is looking for: "
        f"{words}",
        "",
    ]
    lines += [track_line(i, t.fields) for i, t in enumerate(tracks, start=1)]
    questions = {
        f"t{i}": {
            "type": "noul",
            "instructions": f"Does track {i} fit what the DJ is looking for ({words})? Judge track {i} only.",
        }
        for i in range(1, len(tracks) + 1)
    }
    state = "\n".join(lines)
    if len(state) > jev.MAX_STATE_CHARS:
        raise ValueError("AI search shortlist is too long for JEV's context")
    return {"model": model, "state": state, "questions": questions}


def parse_answer(doc: Any, tracks: Sequence[SearchTrack]) -> dict[str, float]:
    """``{stable_id: P(fits)}`` for every track, or :class:`SearchFailed` naming what is missing."""
    answers = doc.get("answers") if isinstance(doc, Mapping) else None
    if not isinstance(answers, Mapping):
        raise SearchFailed("JEV's answer has no answers")
    out: dict[str, float] = {}
    for i, track in enumerate(tracks, start=1):
        ans = answers.get(f"t{i}")
        p = jev._prob(ans.get("noul")) if isinstance(ans, Mapping) else None
        if p is None:
            raise SearchFailed(f"JEV's answer for track {i} is missing or out of range")
        out[track.stable_id] = round(p, 4)
    return out


def rank(
    words: str,
    tracks: Sequence[SearchTrack],
    *,
    key: str | None = None,
    model: str | None = None,
    post: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ask JEV once and return ``{scores, model, cost}``.

    ``post`` is the HTTP call (``jev._http_post``); tests pass a recorded reply.
    """
    key = key if key is not None else os.environ.get(jev.KEY_ENV, "").strip()
    if not key:
        raise SearchKeyMissing(f"AI search needs an OpenRouter key: set {jev.KEY_ENV} in the engine's environment")
    body = build_request(words, tracks, jev.resolve_model(model))
    try:
        doc = (post or jev._http_post)(body, key)
    except Exception as exc:  # urllib, http.client and json errors all mean the call failed
        raise SearchFailed(f"JEV call failed: {type(exc).__name__}: {exc}") from exc
    scores = parse_answer(doc, tracks)
    return {"scores": scores, "model": doc.get("model"), "cost": jev._cost(doc.get("usage"))}


def configured(environ: Mapping[str, str] | None = None) -> bool:
    """Whether an OpenRouter key is set (an empty value counts as unset)."""
    env = os.environ if environ is None else environ
    return bool(env.get(jev.KEY_ENV, "").strip())
