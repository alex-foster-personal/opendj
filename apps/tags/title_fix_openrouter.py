"""Title-fix OpenRouter calls: spend budget, JEV judge and LLM extraction.

Split out of `apps/tags/title_fix.py` (#9686); the mini-PRD for these rules lives in that module's docstring.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx

from .title_fix_model import CFG, Proposal, Track
from .title_fix_vocab import (
    Rx,
    version_tags,
)

# ----------------------------------------------------------------- openrouter


class CallTimeout(RuntimeError):
    """An OpenRouter call passed its wall deadline: CFG.CALL_WALL_S, or the run's wall budget."""

    charged_usd: float = 0.0  # set when the budget charged the abandoned call its reserve


SPEND_CAP_ERROR = "spend cap reached"


T = TypeVar("T")


class PaidCallError(RuntimeError):
    """A call was paid for and its answer then could not be used (missing fields, wrong shape). charged_usd is what
    the budget took for it, so the row's stored verdict accounts for the call instead of reading as free."""

    def __init__(self, message: str, charged_usd: float) -> None:
        super().__init__(message)
        self.charged_usd = charged_usd


def charged_usd(exc: BaseException) -> float:
    """What the spend budget charged for the call that raised ``exc`` (0 when it was never billed)."""
    return float(getattr(exc, "charged_usd", 0.0))


def _parse_billed(data: Any, parse: Callable[[], T]) -> T:
    """Run an answer parser; any failure becomes a PaidCallError carrying the cost the budget took for ``data``."""
    try:
        return parse()
    except Exception as exc:
        raise PaidCallError(str(exc), _reported_cost(data) or 0.0) from exc


class UnknownCostError(RuntimeError):
    """A call got an HTTP 200 and then failed (unreadable body, broken stream): it may have been billed, so its
    reserve is charged rather than released."""

    charged_usd: float = 0.0


class SpendBudget:
    """A run's OpenRouter spend, shared by every worker thread.

    A call RESERVES against the cap before it is dispatched, so calls already in flight count and a scan cannot
    overshoot by the cost of every concurrent call. The estimate is only a guess until a real cost has been seen,
    and a cost is only comparable within one COST CLASS (the model the call is billed on: a cheap JEV call says
    nothing about a DeepSeek call). So the first CFG.PROBE_CALLS calls of each class run one at a time, and only
    once that many have settled may the class fan out. From then on a reservation is the largest cost observed in
    its class (CFG.CONCURRENT_RESERVE_MULT times that when other calls are in flight), never less than the
    estimate, and a call that does not fit beside the calls in flight waits for them. The reserve is settled to
    the real cost on a response, released when the call failed before it could be billed, or kept as the
    charge when the call was abandoned at its wall deadline (its real cost is unknown, so it is never
    free). Without a per-request token cap the cost of a call has no hard upper bound, so a call dearer
    than the multiple of everything seen before it can still pass the cap by its excess."""

    def __init__(self, limit_usd: float) -> None:
        self.limit_usd = limit_usd
        self.spent_usd = 0.0
        self.reserved_usd = 0.0
        self._max_observed_usd: dict[str, float] = {}  # cost class -> largest settled cost
        self._settled: dict[str, int] = {}  # cost class -> calls that settled with a reported cost
        self._in_flight: dict[str, int] = {}
        self._lock = threading.Condition()

    def reserve(self, estimate_usd: float, cost_class: str = "", deadline: float | None = None) -> float:
        """Reserve for one call and return the amount reserved, which the caller must hand back to settle, release
        or abandon (with the same cost_class). Waits while its class's probe call is in flight, and while other
        calls are in flight and the headroom cannot cover this one on top of them (calls near the cap run one at a
        time, so a call dearer than its predecessors overshoots by its own excess only). Raises SpendCapReached
        when nothing is in flight and the cap still leaves no room. Without a ``deadline`` the wait has no timeout:
        every call in flight ends within the call wall (settled, released or abandoned) and each of those wakes the
        waiters, so admission always comes; a fixed timeout would drop rows while a provider recovers behind abandoned
        probes, which never advance the probe count. With a ``deadline`` (a time.monotonic() stamp, the run's
        whole-run wall budget) it never waits past it."""
        with self._lock:
            wait_s = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not self._lock.wait_for(lambda: self._admissible(estimate_usd, cost_class), timeout=wait_s):
                raise CallTimeout("the spend budget did not free up within the run's wall budget")
            amount = self._amount(estimate_usd, cost_class)
            if not self._fits(amount):
                raise SpendCapReached(
                    f"{SPEND_CAP_ERROR}: ${self.spent_usd:.4f} spent and ${self.reserved_usd:.4f} in flight "
                    f"of ${self.limit_usd:.4f}"
                )
            self.reserved_usd += amount
            self._in_flight[cost_class] = self._in_flight.get(cost_class, 0) + 1
            return amount

    def _amount(self, estimate_usd: float, cost_class: str) -> float:
        observed = self._max_observed_usd.get(cost_class, 0.0)
        beside_others = self._any_in_flight()
        return max(estimate_usd, observed * (CFG.CONCURRENT_RESERVE_MULT if beside_others else 1.0))

    def _fits(self, amount_usd: float) -> bool:
        return self.spent_usd + self.reserved_usd + amount_usd <= self.limit_usd

    def _admissible(self, estimate_usd: float, cost_class: str) -> bool:
        probed = self._settled.get(cost_class, 0) >= CFG.PROBE_CALLS or self._in_flight.get(cost_class, 0) == 0
        return probed and (self._fits(self._amount(estimate_usd, cost_class)) or not self._any_in_flight())

    def settle(self, reserved_usd: float, actual_usd: float, cost_class: str = "") -> None:
        with self._lock:
            self._return_reserve(reserved_usd, cost_class)
            self.spent_usd += actual_usd
            self._max_observed_usd[cost_class] = max(self._max_observed_usd.get(cost_class, 0.0), actual_usd)
            self._settled[cost_class] = self._settled.get(cost_class, 0) + 1
            self._lock.notify_all()

    def release(self, reserved_usd: float, cost_class: str = "") -> None:
        with self._lock:
            self._return_reserve(reserved_usd, cost_class)
            self._lock.notify_all()

    def abandon(self, reserved_usd: float, cost_class: str = "") -> None:
        """Charge the reserve and learn nothing: an abandoned call never showed its real cost, so it does not end
        the probe phase and the next call of its class is dispatched alone."""
        with self._lock:
            self._return_reserve(reserved_usd, cost_class)
            self.spent_usd += reserved_usd
            self._lock.notify_all()

    def _any_in_flight(self) -> bool:
        return sum(self._in_flight.values()) > 0

    def _return_reserve(self, reserved_usd: float, cost_class: str) -> None:
        self.reserved_usd -= reserved_usd
        self._in_flight[cost_class] -= 1
        if not self._any_in_flight():
            self.reserved_usd = 0.0  # nothing is held: shed the float residue of unequal reservations coming back


class SpendCapReached(RuntimeError):
    """The run's spend budget was spent before this call started: the row is left unknown, not guessed."""


@dataclass(frozen=True)
class Api:
    """How a row reaches OpenRouter. key None reads $OPENROUTER_API_KEY at call time (the CLI and eval
    default); deadline is a time.monotonic() stamp after which no call starts (the run's wall budget)."""

    key: str | None = None
    deadline: float | None = None
    budget: SpendBudget | None = None

    def __post_init__(self) -> None:
        if self.key is not None and not self.key.strip():
            raise ValueError("api_key was passed but is blank; pass None to read the environment")


def _api_key(api: Api) -> str:
    key = (api.key if api.key is not None else os.environ.get(CFG.OPENROUTER_KEY_ENV, "")).strip()
    if not key:
        raise RuntimeError(
            f"{CFG.OPENROUTER_KEY_ENV} is not set and no api_key was passed; title fixing needs OpenRouter"
        )
    return key


def _call_wall_s(api: Api) -> float:
    if api.deadline is None:
        return CFG.CALL_WALL_S
    left = api.deadline - time.monotonic()
    if left <= 0:
        raise CallTimeout("run wall budget spent before this call started")
    return min(CFG.CALL_WALL_S, left)


def _read_response(url: str, body: dict[str, Any], key: str, stop: threading.Event) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {key}", "X-Title": "Open DJ"}
    with httpx.stream("POST", url, json=body, headers=headers, timeout=CFG.TIMEOUT_S) as response:
        chunks: list[bytes] = []
        try:
            for chunk in response.iter_bytes():
                if stop.is_set():  # abandoned at the wall deadline: leaving the block closes the stream
                    break
                chunks.append(chunk)
        except Exception as exc:
            if response.status_code == 200:
                raise UnknownCostError(f"{url} answered 200 and the stream then failed: {exc}") from exc
            raise
    if stop.is_set():
        raise CallTimeout(f"{url} abandoned at its wall deadline")
    raw = b"".join(chunks)
    if response.status_code != 200:
        raise RuntimeError(f"{url} returned HTTP {response.status_code}: {raw[:200].decode(errors='replace')}")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise UnknownCostError(f"{url} answered 200 with an unreadable body: {exc}") from exc


def _post(url: str, body: dict[str, Any], api: Api) -> dict[str, Any]:
    """One OpenRouter call under a HARD wall deadline. httpx's timeout bounds each socket read, and OpenRouter
    keeps a slow call alive with whitespace, so a stalled call never trips it: the call runs on a daemon thread
    that is abandoned at the deadline and closes its own stream on the next chunk."""
    key, cost_class = _api_key(api), str(body["model"])
    reserve = 0.0 if api.budget is None else api.budget.reserve(CFG.CALL_RESERVE_USD, cost_class, api.deadline)
    try:
        wall = _call_wall_s(api)  # after the reservation wait: the run's deadline may have passed behind a probe
    except CallTimeout:
        if api.budget is not None:
            api.budget.release(reserve, cost_class)
        raise
    outcome: dict[str, Any] = {}
    stop = threading.Event()

    def call() -> None:
        try:
            outcome["data"] = _read_response(url, body, key, stop)
        except Exception as exc:  # noqa: BLE001 - not swallowed: re-raised on the caller thread below
            outcome["error"] = exc

    worker = threading.Thread(target=call, name="openrouter-call", daemon=True)
    worker.start()
    worker.join(wall)
    if worker.is_alive():
        stop.set()
        if api.budget is not None:
            api.budget.abandon(reserve, cost_class)  # dispatched and possibly billed: charged its reserve, never free
        timeout = CallTimeout(f"{url} passed its {wall:.0f} s wall deadline")
        timeout.charged_usd = reserve
        raise timeout
    if "error" in outcome:
        if api.budget is not None:
            if isinstance(outcome["error"], UnknownCostError):
                api.budget.abandon(reserve, cost_class)  # a 200 was returned: possibly billed, so never free
                outcome["error"].charged_usd = reserve
            else:
                api.budget.release(reserve, cost_class)  # failed before a response: nothing was billed
        raise outcome["error"]
    if api.budget is not None:
        _settle_response(api.budget, reserve, cost_class, url, outcome["data"])
    return outcome["data"]


def _settle_response(budget: SpendBudget, reserve: float, cost_class: str, url: str, data: Any) -> None:
    """Settle a response to the cost it reports. One that reports none is charged its reserve (a billable call is never
    free) and then FAILS the row with a PaidCallError carrying that charge: the response is never rewritten into a
    success with an invented cost."""
    cost = _reported_cost(data)
    if cost is not None:
        budget.settle(reserve, cost, cost_class)
        return
    budget.abandon(reserve, cost_class)
    shape = (
        "a JSON object without a valid usage.cost"
        if isinstance(data, dict)
        else f"a {type(data).__name__}, not a JSON object"
    )
    raise PaidCallError(f"{url} answered 200 with {shape}", reserve)


def _reported_cost(data: Any) -> float | None:
    """usage.cost as a finite, non-negative number, else None (unknown: the caller charges its reserve)."""
    usage = data.get("usage") if isinstance(data, dict) else None
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        return None
    return float(cost)


def _required_cost(data: Any) -> float:
    """usage.cost when it is a finite non-negative number; anything else fails the row instead of reading as free."""
    cost = _reported_cost(data)
    if cost is None:
        raise ValueError("the response reports no valid usage.cost")
    return cost


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
