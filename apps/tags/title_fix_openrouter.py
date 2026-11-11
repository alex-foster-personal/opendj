"""Title-fix OpenRouter calls: JEV judge and LLM extraction.

Split out of `apps/tags/title_fix.py` (#9686). OpenRouter spend cap and HTTP wall-deadline calls live in
`title_fix_spend.py` (#11226); the mini-PRD for these rules lives in the facade module's docstring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .title_fix_model import CFG, Proposal, Track
from .title_fix_spend import (  # re-exported: public spend surface for apps.tags.title_fix
    SPEND_CAP_ERROR,
    Api,
    CallTimeout,
    PaidCallError,
    SpendBudget,
    SpendCapReached,
    T,
    UnknownCostError,
    _api_key,
    _parse_billed,
    _post,
    _required_cost,
    charged_usd,
)
from .title_fix_vocab import (
    Rx,
    version_tags,
)

# ----------------------------------------------------------------- openrouter (jev / llm)


def _describe(track: Track, proposal: Proposal) -> str:
    return (
        f"RAW title: {track.title}\nRAW artist: {track.artist or '(blank)'}\n"
        f"filename: {track.filename}\n\n"
        f"PROPOSED artist: {proposal.artist or '(blank)'}\nPROPOSED title: {proposal.title}"
        + (
            f"\nPROPOSED producer (credit removed from the title, recorded on the proposal only): {proposal.producer}"
            if proposal.producer
            else ""
        )
    )


JEV_QUESTIONS: dict[str, Any] = {
    "kind": {
        "type": "choice",
        "instructions": "What is this library row?",
        "criteria": {
            "music": "a song or DJ track by an artist, including remixes, edits, live sets",
            "not_music": "a one-shot sample (CLAP, HORN), a recording capture, a streaming URI, "
            "or a DJ's own unreleased project file with no real song identity",
        },
    },
    "proposal_ok": {
        "type": "noul",
        "instructions": "Are the PROPOSED artist and PROPOSED title both exactly right for this "
        "song? Title must keep remix/edit/version names, featured artists and lyric or translation "
        "notes like '(Lyrics)' or '(English lyrics translation)', but drop file "
        "junk (bitrates, [Official Video], site names, track numbers, key/energy tags). Artist "
        "must be the performing artist, never blank when the raw text names one. Answer no if "
        "any word of the real title was lost or the artist is wrong or missing.",
    },
}


MASHUP_QUESTION: dict[str, Any] = {
    "type": "noul",
    "instructions": "Is this row a MASHUP: one track that blends two or more DIFFERENT existing songs, "
    "e.g. 'Ember X Pale Hours' or 'Song A vs Song B (DJ Mashup)'? Answer NO when ' x ', 'X' or "
    "'vs' joins ARTIST names in a collaboration or a remix credit, e.g. 'Kestrel Fold x Hollow "
    "Parade - Lights Out Again', 'Last Night - Vantor Quill x Lio Marrow Remix', 'Waste (Q Z Remix)', "
    "or 'We Are Neighbors - Tallow Vs. Marrow'.",
}


@dataclass(frozen=True)
class JevAnswer:
    kind: str
    p_ok: float
    p_mashup: float | None
    cost: float


def mashup_candidate(track: Track, proposal: Proposal) -> bool:
    """ " x " / " vs " in the title with no explicit mashup word: only JEV can tell a mashup from a collab."""
    return "mashup" not in proposal.tags and bool(Rx.MASHUP_JOIN.search(f"{track.title} | {proposal.title}"))


def judge_with_jev(track: Track, proposal: Proposal, api: Api) -> JevAnswer:
    """One JEV call: kind, P(proposal ok) and, for " x " candidates only, P(mashup). Raises on any failure."""
    ask_mashup = mashup_candidate(track, proposal)
    questions = {**JEV_QUESTIONS, "mashup": MASHUP_QUESTION} if ask_mashup else JEV_QUESTIONS
    data = _post(
        CFG.JEV_URL, {"model": CFG.JEV_MODEL, "state": _describe(track, proposal), "questions": questions}, api
    )
    return _parse_billed(data, lambda: _jev_answer(data, ask_mashup))


def _probability(value: object, what: str) -> float:
    """A model-reported probability: a number in [0, 1] (NaN and infinity fall outside it), never a bool (float(True) would read as certain)."""
    if (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0
    ):  # the range test is also False for NaN
        raise ValueError(f"{what} is not a probability in [0, 1]: {value!r}")
    return float(value)


def _jev_answer(data: dict[str, Any], ask_mashup: bool) -> JevAnswer:
    answers = data["answers"]
    kind_probs = answers["kind"]["probabilities"]
    p_not_music = _probability(kind_probs["not_music"], "kind.not_music")  # required: a missing one is no answer
    p_music = _probability(kind_probs["music"], "kind.music")
    if abs(p_music + p_not_music - 1.0) > CFG.KIND_SUM_TOLERANCE:
        raise ValueError(f"kind probabilities are not a distribution: music {p_music} + not_music {p_not_music}")
    kind = "not_music" if p_not_music >= CFG.NOT_MUSIC_THRESHOLD else "music"
    p_mashup = _probability(answers["mashup"]["noul"], "mashup.noul") if ask_mashup else None
    p_ok = _probability(answers["proposal_ok"]["noul"], "proposal_ok.noul")
    return JevAnswer(kind, p_ok, p_mashup, _required_cost(data))


LLM_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "track_identity",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"artist": {"type": "string"}, "title": {"type": "string"}},
            "required": ["artist", "title"],
            "additionalProperties": False,
        },
    },
}

LLM_SYSTEM = (
    "You clean DJ library metadata. Return the performing artist(s) and the song title. "
    "Keep remix/edit/version names and featured artists in the title, e.g. "
    "'Opal - Marrow Remix' or 'Love Song (feat. Glasses)'. Keep lyric or translation notes in the title too, "
    "e.g. '(Lyrics)', '(Lyric Video)' or '(English lyrics translation)'. Drop file junk: bitrates, "
    "[Official Video], site names, track numbers, Mixed In Key energy/key tags like '6 - ' "
    "or '9A - 7 - ', trailing key/bpm like ' - Fm - 125', '_capture', '(1)', and producer credits like "
    "' - prod. X' or '(prod. by X)'. "
    "If no artist is named anywhere, return an empty artist string: never invent or "
    "guess one and never write 'Unknown'. "
    "When RAW artist is filled, never drop leading words of the raw title unless they repeat "
    "that artist: 'Evergreen - Quiet People (Club Mix) [Mixed]' keeps 'Evergreen - ' and '[Mixed]'. "
    "In a DJ project or bounce file, a leading NAME_ prefix names the artist, e.g. "
    "'VESPR_Lowtide Breaks AA 1644 MASTER' is artist 'VESPR', title 'Lowtide Breaks'."
)


def extract_with_llm(track: Track, api: Api) -> tuple[Proposal, float]:
    data = _post(
        CFG.CHAT_URL,
        {
            "model": CFG.FALLBACK_MODEL,
            "messages": [
                {"role": "system", "content": LLM_SYSTEM},
                {"role": "user", "content": _describe(track, Proposal("", "")).split("\n\n")[0]},
            ],
            "response_format": LLM_SCHEMA,
            "temperature": 0,
            "usage": {"include": True},
        },
        api,
    )
    return _parse_billed(data, lambda: _llm_proposal(track, data))


def _llm_proposal(track: Track, data: dict[str, Any]) -> tuple[Proposal, float]:
    content = data["choices"][0]["message"].get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(
            f"{CFG.FALLBACK_MODEL} returned no content (finish={data['choices'][0].get('finish_reason')})"
        )
    parsed = json.loads(content)
    cost = _required_cost(data)
    # A filled artist field is the user's data: the LLM only ever fixes the title then.
    artist = track.artist.strip() or parsed["artist"].strip()
    title = parsed["title"].strip()
    return Proposal(artist=artist, title=title, tags=version_tags(title)), cost
