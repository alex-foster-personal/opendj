"""Genre and tag GUESSES from JEV, a fast decisions model on OpenRouter.

WHAT IT IS. JEV (``~typesafe/jev-latest`` on ``/api/alpha/decisions``) takes a
block of text and named questions and returns a probability per answer in
about half a second for well under a hundredth of a cent. Here the text is
what the library already knows about a track (artist, title, album, label,
BPM, key, file name) and the questions are:

* ``genre``: a choice over the wheel's simple genre FAMILIES
  (``apps/library_wheel/genre_families.py``) plus ``other``;
* one yes/no question per user tag in ``<data>/state/genre/jev_tags.json``
  (``[{"name": "warmup", "question": "Is this a warm-up track?"}]``), asked
  in the SAME call, because JEV charges per call, not per question.

WHAT IT IS NOT. A guess is never a tag. Nothing here writes to an audio file,
to ``track_fields`` or to rekordbox; results go to the machine-local sidecar
``<data>/state/genre/jev_suggestions.json`` and the library row serves them as
``genre_guess`` only while the track has no genre tag of its own.

HONEST ACCURACY. Measured Thu 1 Oct 2026 on 100 random tracks of the owner's
library from artist and title alone, JEV agreed with Sonnet 5.5 (low effort,
same inputs) on 42 to 46 of 100 and was confident (>= 0.8) on only 4 to 6.
JEV judges the text it can see and does not know underground artists, so the
row only shows a guess at or above ``min_confidence`` and always says it is
one. The yes/no tag questions are the stronger use: they judge what is in the
text rather than recall an artist.

FAILURES ARE UNKNOWN. No key, a failed call or a malformed answer becomes
``status: unknown`` with a reason, never a family.

-Claude
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "~typesafe/jev-latest"
KEY_ENV = "OPENROUTER_API_KEY"
MODEL_ENV = "JEV_MODEL"
SCHEMA = "jev-suggestions/v1"
# JEV's context is 32k tokens; estimate 3.5 characters a token and refuse
# anything that would not fit with headroom instead of letting it truncate.
MAX_STATE_CHARS = int(30000 * 3.5)
# Below this the row shows no guess: the 1 Oct 2026 run was right on most of
# its few >= 0.8 answers and near chance below.
DEFAULT_MIN_CONFIDENCE = 0.8

# One line per family. The text is the model: say what belongs in each class.
FAMILY_CRITERIA: dict[str, str] = {
    "drum-and-bass": "drum and bass, jungle, liquid, neurofunk, rollers (usually 160 to 180 BPM)",
    "breakbeat-garage": "breakbeat, breaks, UK garage, 2-step, speed garage, bassline",
    "techno": "techno, industrial, schranz, peak time, hard techno, minimal or dub techno",
    "tech-house": "tech house",
    "deep-house": "deep house, organic house, melodic house",
    "house": "house, afro house, jackin, funky or disco house, vocal house, amapiano",
    "trance": "trance, psytrance, progressive trance, uplifting trance",
    "hardstyle": "hardstyle, hardcore, gabber, frenchcore, hard dance",
    "dubstep": "dubstep, bass music, riddim, tearout, future bass",
    "hip-hop": "hip hop, rap, trap (rap), grime, drill",
    "disco-funk-soul": "disco, nu-disco, boogie, funk, soul, R&B",
    "ambient-downtempo": "ambient, downtempo, chillout, lo-fi, trip-hop",
    "electro-edm": "electro, EDM, big room, festival or mainstage dance",
    "reggae": "reggae, dancehall, dub, ska",
    "jazz-latin-world": "jazz, latin, salsa, reggaeton, afrobeats, world music",
    "pop-rock-indie": "pop, rock, indie, metal, songs that are not dance music",
    "other": "none of the above, or there is not enough information to tell",
}

GENRE_INSTRUCTIONS = [
    "The STATE describes one track in a DJ's music library: whatever artist, title, album, "
    "label, tempo, key and file name the library knows.",
    "Which genre family does this track belong to? Use what you know about the artist and "
    "the words you can see. Answer other when nothing points to a family.",
]

# Fields read into the state, in this order, with the label each gets.
_STATE_FIELDS = (
    ("artist", "Artist"),
    ("title", "Title"),
    ("album", "Album"),
    ("label", "Label"),
    ("year", "Year"),
    ("bpm", "BPM"),
    ("key", "Key"),
    ("comments", "Comments"),
    ("file_name", "File name"),
)


@dataclass(frozen=True)
class TagQuestion:
    name: str
    question: str


Transport = Callable[[dict[str, Any], str], dict[str, Any]]


def build_state(fields: Mapping[str, Any]) -> str:
    """The text JEV judges: one ``Label: value`` line per known field, empties left out.

    A genre tag is never part of it: guesses are only made for untagged tracks,
    and a tag in the state would just be read back.
    """
    lines = []
    for key, label in _STATE_FIELDS:
        value = fields.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        lines.append(f"{label}: {value}")
    return "\n".join(lines)


def load_tag_questions(path: Path) -> list[TagQuestion]:
    """The user's yes/no tag questions, or none when the file does not exist."""
    if not path.is_file():
        return []
    raw = json.loads(path.read_text())
    out: list[TagQuestion] = []
    seen: set[str] = set()
    for item in raw:
        name, question = str(item["name"]).strip(), str(item["question"]).strip()
        if not name or not question or name == "genre" or name in seen:
            raise ValueError(f"bad tag question {item!r}: needs a unique name (not 'genre') and a question")
        seen.add(name)
        out.append(TagQuestion(name, question))
    return out


def build_request(state: str, tags: Iterable[TagQuestion], model: str) -> dict[str, Any]:
    questions: dict[str, Any] = {
        "genre": {"type": "choice", "instructions": GENRE_INSTRUCTIONS, "criteria": FAMILY_CRITERIA}
    }
    for tag in tags:
        questions[f"tag:{tag.name}"] = {"type": "noul", "instructions": tag.question}
    return {"model": model, "state": state, "questions": questions}


def _http_post(body: dict[str, Any], key: str) -> dict[str, Any]:
    req = urllib.request.Request(
        DECISIONS_URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _unknown(reason: str) -> dict[str, Any]:
    return {"status": "unknown", "reason": reason}


def _tag_answers(answers: Mapping[str, Any], tags: Iterable[TagQuestion]) -> dict[str, float] | str:
    """P(yes) per tag question, or the reason one is missing or out of range."""
    out: dict[str, float] = {}
    for tag in tags:
        ans = answers.get(f"tag:{tag.name}")
        p = ans.get("noul") if isinstance(ans, Mapping) else None
        if not isinstance(p, int | float) or not 0.0 <= float(p) <= 1.0:
            return f"tag {tag.name!r} answer missing or out of range"
        out[tag.name] = round(float(p), 4)
    return out


def _genre_answer(answers: Any) -> tuple[str, float, Mapping[str, Any]] | str:
    """(family, confidence, probabilities), or the reason the genre answer is unusable."""
    if not isinstance(answers, Mapping):
        return "response has no answers"
    genre = answers.get("genre")
    if not isinstance(genre, Mapping):
        return "no genre answer"
    family, probs = genre.get("choice"), genre.get("probabilities")
    if family not in FAMILY_CRITERIA or not isinstance(probs, Mapping):
        return f"genre answer {family!r} is not a family"
    confidence = probs.get(family)
    if not isinstance(confidence, int | float) or not 0.0 <= float(confidence) <= 1.0:
        return "genre confidence missing or out of range"
    return str(family), float(confidence), probs


def parse_answer(doc: Mapping[str, Any], tags: Iterable[TagQuestion]) -> dict[str, Any]:
    """One JEV response -> a suggestion, or ``unknown`` when any part is missing or out of range."""
    genre = _genre_answer(doc.get("answers"))
    if isinstance(genre, str):
        return _unknown(genre)
    family, confidence, probs = genre
    tag_p = _tag_answers(doc["answers"], tags)
    if isinstance(tag_p, str):
        return _unknown(tag_p)
    return {
        "status": "ok",
        "family": family,
        "confidence": round(confidence, 4),
        "probabilities": {k: round(float(v), 4) for k, v in probs.items() if isinstance(v, int | float)},
        "tags": tag_p,
        "model": doc.get("model"),
        "cost": (doc.get("usage") or {}).get("cost"),
    }


def classify(
    states: Mapping[str, str],
    tags: list[TagQuestion],
    *,
    key: str | None = None,
    model: str | None = None,
    transport: Transport | None = None,
    workers: int = 8,
) -> dict[str, dict[str, Any]]:
    """Ask JEV about every track in ``states`` (stable_id -> state text), one call per track."""
    key = key if key is not None else os.environ.get(KEY_ENV, "")
    model = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
    send = transport or _http_post
    if not key:
        return {sid: _unknown(f"no {KEY_ENV} in the environment") for sid in states}

    def one(item: tuple[str, str]) -> tuple[str, dict[str, Any]]:
        sid, state = item
        if not state.strip():
            return sid, _unknown("nothing known about this track to judge")
        if len(state) > MAX_STATE_CHARS:
            return sid, _unknown("state too large for JEV's context")
        try:
            return sid, parse_answer(send(build_request(state, tags, model), key), tags)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            return sid, _unknown(f"call failed: {type(exc).__name__}")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return dict(pool.map(one, states.items()))


__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_MODEL",
    "FAMILY_CRITERIA",
    "KEY_ENV",
    "SCHEMA",
    "TagQuestion",
    "build_request",
    "build_state",
    "classify",
    "load_tag_questions",
    "parse_answer",
]
