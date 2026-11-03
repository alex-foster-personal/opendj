"""The network calls behind genre inference: one web search, JEV and DeepSeek, and the fusion rule.

Calibrated in ``(private path)`` (rounds 5, 10, 11):

* **Search** (Serper, Google results): the FROZEN query ``"{artist} {title} genre"``,
  ``num=10``, first 6 results as ``title: snippet`` lines. Search is the single
  largest lever measured (round 12: +12 points for DeepSeek, +25 for JEV).
* **JEV** (``~typesafe/jev-latest`` on OpenRouter's decisions endpoint): a
  ``choice`` question over the taxonomy; the FULL probability vector is kept.
* **DeepSeek V4.1 Flash** (OpenRouter chat, ``reasoning`` explicitly off, round 10:
  default reasoning cost about 6x for no gain): one genre and a stated confidence.
* **Fusion**: DeepSeek when its confidence is at least 0.9, else the equal-weight
  average of JEV's vector and DeepSeek's answer spread as a distribution.

Audio models are deliberately absent: the calibrated audio head (Discogs-EffNet)
is CC BY-NC-SA, which an Apache product cannot ship. :class:`GenreProvider` is
the seam an audio provider plugs into later.

No key is a hard error naming the key (:class:`ProviderUnavailable`), never a
silent skip. A failed or malformed answer is ``unknown`` with a reason, never a genre.

-Claude
"""

from __future__ import annotations

import http.client
import json
import math
import os
import random
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from . import keystore, taxonomy
from .probability import unit_probability as _prob

# ----------------------------------------------------------------- config
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
SERPER_KEY_ENV = "SERPER_API_KEY"
KEY_NAMES = (OPENROUTER_KEY_ENV, SERPER_KEY_ENV)
# Where calls go. The overrides exist so tests point the providers at a real
# local HTTP server instead of the internet.
OPENROUTER_ROOT_ENV = "MDT_GENRE_OPENROUTER_ROOT"
SERPER_URL_ENV = "MDT_GENRE_SERPER_URL"
DEFAULT_OPENROUTER_ROOT = "https://openrouter.ai/api"
DEFAULT_SERPER_URL = "https://google.serper.dev/search"
JEV_MODEL = "~typesafe/jev-latest"
DEEPSEEK_MODEL = "deepseek/deepseek-v4.1-flash"
PROMPT_VERSION = "v3-baseline/1"
# FROZEN (owner, Mon 12 Oct 2026): the search sent per track must not change
# unless the owner says so. A change needs a new QUERY_VERSION and the owner's
# go-ahead; the assertion refuses a silent edit.
QUERY_VERSION = "q1"
QUERY_TEMPLATE = "{artist} {title} genre"
QUERY_PARAMS: dict[str, Any] = {"num": 10}
SNIPPET_LINES = 6
assert (QUERY_VERSION, QUERY_TEMPLATE, QUERY_PARAMS) == ("q1", "{artist} {title} genre", {"num": 10}), (
    "the genre search query is frozen; ask the owner before changing it"
)
# Serper bills per search, not per token: $0.30 to $1.00 per 1,000 searches by
# plan (free credits cost nothing). The default is the worst published price so
# the cost guard never under-reports; a user on a cheaper plan sets the env var.
SERPER_PRICE_ENV = "MDT_GENRE_SERPER_USD_PER_SEARCH"
SERPER_USD_PER_SEARCH_DEFAULT = 0.001


def serper_usd_per_search(environ: Mapping[str, str] | None = None) -> float:
    raw = (environ if environ is not None else os.environ).get(SERPER_PRICE_ENV, "").strip()
    if not raw:
        return SERPER_USD_PER_SEARCH_DEFAULT
    value = float(raw)
    if not 0 <= value < 1:
        raise ValueError(f"{SERPER_PRICE_ENV}={raw!r} must be a per-search price in dollars, 0 to 1")
    return value


DEEPSEEK_SURE = 0.9
# A choice answer's probabilities sum to 1; a vector off by more than this is malformed.
VECTOR_SUM_TOLERANCE = 0.05
BLEND_JEV_WEIGHT = 0.5
HTTP_TIMEOUT_S = 60
MAX_ATTEMPTS = 4


class ProviderUnavailable(RuntimeError):
    """A provider cannot run at all (no key). ``key_env`` names what to set."""

    def __init__(self, key_env: str) -> None:
        super().__init__(
            f"{key_env} is not set: genre inference needs it "
            f"(set it with `python -m apps.genre_infer keys set {key_env}` or in the engine's environment)"
        )
        self.key_env = key_env


class ProviderCallError(RuntimeError):
    """One call failed after its retries."""


class ResponseInvalid(ProviderCallError):
    """The service answered (and may have billed) but its body is unusable."""


def require_key(env: str, environ: Mapping[str, str] | None = None) -> str:
    """The non-blank value of ``env``; a blank value is someone clearing the key."""
    value = keystore.resolve(env, environ)
    if not value:
        raise ProviderUnavailable(env)
    return value


def missing_keys(environ: Mapping[str, str] | None = None) -> list[str]:
    return [k for k in KEY_NAMES if not keystore.resolve(k, environ)]


# ------------------------------------------------------------------- http
def _openrouter(path: str) -> str:
    root = os.environ.get(OPENROUTER_ROOT_ENV, "").strip() or DEFAULT_OPENROUTER_ROOT
    return root.rstrip("/") + path


def _serper_url() -> str:
    return os.environ.get(SERPER_URL_ENV, "").strip() or DEFAULT_SERPER_URL


def _post_json(url: str, body: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
    """POST with retries on 429, 5xx and network errors; anything else fails at once."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}
    )
    last = ""
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code != 429 and exc.code < 500:
                break
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            last = f"{type(exc).__name__}"
        else:
            # The service answered (and may have billed): an unusable body is not
            # retried, so one call is never silently paid for several times.
            try:
                doc = json.loads(raw)
            except ValueError as exc:
                raise ResponseInvalid(f"{url} answered with a body that is not JSON") from exc
            if not isinstance(doc, dict):
                raise ResponseInvalid(f"{url} answered with JSON that is not an object")
            return doc
        time.sleep(min(30.0, 2.0**attempt) + random.random())
    raise ProviderCallError(f"{url} failed: {last}")


def _cost(doc: Mapping[str, Any]) -> float | None:
    usage = doc.get("usage")
    cost = usage.get("cost") if isinstance(usage, Mapping) else None
    if isinstance(cost, bool) or not isinstance(cost, int | float) or not math.isfinite(float(cost)):
        return None
    return float(cost)


# ----------------------------------------------------------------- search
def search_query(artist: str, title: str) -> str:
    return QUERY_TEMPLATE.format(artist=artist, title=title)


def web_search(query: str, key: str) -> str:
    """The first :data:`SNIPPET_LINES` Google results as ``title: snippet`` lines (may be empty)."""
    doc = _post_json(_serper_url(), {"q": query, **QUERY_PARAMS}, {"X-API-KEY": key})
    organic = doc.get("organic")
    if not isinstance(organic, list):
        raise ResponseInvalid("search response has no organic results list")
    return "\n".join(
        f"{r.get('title', '')}: {r.get('snippet', '')}" for r in organic[:SNIPPET_LINES] if isinstance(r, dict)
    )


# -------------------------------------------------------------- answers
@dataclass(frozen=True)
class Answer:
    """One provider's answer for one track. ``genre`` is None when it is unknown."""

    provider: str
    genre: str | None
    confidence: float | None
    probabilities: Mapping[str, float] | None
    model: str | None
    cost_usd: float | None
    reason: str | None = None
    # True when the call itself failed (network, 429, 5xx): worth asking again,
    # unlike a model that answered but gave nothing usable.
    transient: bool = False


class GenreProvider(Protocol):
    """The seam for another genre source (an audio model later): state text in, answer out."""

    name: str

    def answer(self, state: str) -> Answer: ...


def _unknown(provider: str, reason: str, *, model: str | None = None, cost: float | None = None) -> Answer:
    return Answer(provider, None, None, None, model, cost, reason)


class JevProvider:
    name = "jev"

    def __init__(self, key: str, model: str = JEV_MODEL) -> None:
        self.key, self.model = key, model

    def answer(self, state: str) -> Answer:
        body = {
            "model": self.model,
            "state": state,
            "questions": {
                "genre": {"type": "choice", "instructions": list(taxonomy.INSTRUCTIONS), "criteria": taxonomy.GENRES}
            },
        }
        try:
            doc = _post_json(_openrouter("/alpha/decisions"), body, {"Authorization": f"Bearer {self.key}"})
        except ProviderCallError as exc:
            return Answer(self.name, None, None, None, self.model, None, str(exc), transient=True)
        answers = doc.get("answers")
        genre = answers.get("genre") if isinstance(answers, Mapping) else None
        probs = genre.get("probabilities") if isinstance(genre, Mapping) else None
        if not isinstance(probs, Mapping):
            return _unknown(self.name, "no genre probabilities in the answer", model=doc.get("model"), cost=_cost(doc))
        vector = {g: p for g in taxonomy.NAMES if (p := _prob(probs.get(g))) is not None}
        if len(vector) != len(taxonomy.NAMES) or abs(sum(vector.values()) - 1.0) > VECTOR_SUM_TOLERANCE:
            return _unknown(
                self.name,
                "probability vector does not cover the taxonomy or does not sum to 1",
                model=doc.get("model"),
                cost=_cost(doc),
            )
        top = max(vector, key=lambda g: vector[g])
        return Answer(self.name, top, round(vector[top], 4), vector, str(doc.get("model") or self.model), _cost(doc))


def _deepseek_system() -> str:
    lines = "\n".join(f"- {g}: {d}" for g, d in taxonomy.GENRES.items())
    return (
        "\n".join(taxonomy.INSTRUCTIONS)
        + '\n\nChoose exactly one genre from this list (or "other" if none fits):\n'
        + lines
        + "\n- other: none of the above\n\n"
        + 'Reply with strict JSON only: {"genre": "<one name from the list, copied exactly>", "confidence": <0 to 1>}'
    )


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, key: str, model: str = DEEPSEEK_MODEL) -> None:
        self.key, self.model = key, model
        self.system = _deepseek_system()

    def answer(self, state: str) -> Answer:
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": 400,
            "usage": {"include": True},
            "reasoning": {"enabled": False},
            "messages": [{"role": "system", "content": self.system}, {"role": "user", "content": "STATE:\n" + state}],
        }
        try:
            doc = _post_json(_openrouter("/v1/chat/completions"), body, {"Authorization": f"Bearer {self.key}"})
        except ProviderCallError as exc:
            return Answer(self.name, None, None, None, self.model, None, str(exc), transient=True)
        cost, model = _cost(doc), str(doc.get("model") or self.model)
        parsed = _chat_json(doc)
        if isinstance(parsed, str):
            return _unknown(self.name, parsed, model=model, cost=cost)
        genre = taxonomy.canonical(str(parsed.get("genre") or ""))
        confidence = _prob(parsed.get("confidence"))
        if genre is None:
            return _unknown(
                self.name, f"answered {str(parsed.get('genre'))[:40]!r}, not a taxonomy genre", model=model, cost=cost
            )
        if confidence is None:
            return _unknown(self.name, "confidence missing or out of range", model=model, cost=cost)
        return Answer(self.name, genre, round(confidence, 4), None, model, cost)


def _chat_json(doc: Mapping[str, Any]) -> dict[str, Any] | str:
    """The first chat choice's content as a JSON object, or the reason it is not one."""
    choices = doc.get("choices")
    message = (
        choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    )
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        return "empty answer"
    try:
        parsed = json.loads(content.strip().removeprefix("```json").removesuffix("```"))
    except ValueError:
        return "answer is not JSON"
    if not isinstance(parsed, dict):
        return "answer is not a JSON object"
    return parsed


# ------------------------------------------------------------------ fusion
def answer_distribution(answer: Answer) -> dict[str, float]:
    """A one-genre answer spread over the taxonomy: its confidence on the genre, the rest even."""
    k = len(taxonomy.NAMES)
    if answer.genre is None or answer.confidence is None:
        return {g: 1.0 / k for g in taxonomy.NAMES}
    c = min(max(answer.confidence, 1e-3), 1 - 1e-3)
    return {g: (c if g == answer.genre else (1 - c) / (k - 1)) for g in taxonomy.NAMES}


@dataclass(frozen=True)
class Fused:
    genre: str | None
    confidence: float | None
    rule: str


def fuse(jev: Answer, deepseek: Answer) -> Fused:
    """Round 11's text-only rule: DeepSeek when sure (>= 0.9), else the JEV + DeepSeek average."""
    if deepseek.genre is not None and deepseek.confidence is not None and deepseek.confidence >= DEEPSEEK_SURE:
        return Fused(deepseek.genre, deepseek.confidence, f"deepseek>={DEEPSEEK_SURE}")
    if jev.probabilities is None and deepseek.genre is None:
        return Fused(None, None, "both unknown")
    if jev.probabilities is None:
        return Fused(deepseek.genre, deepseek.confidence, "deepseek only (jev unknown)")
    spread = answer_distribution(deepseek)
    blend = {
        g: BLEND_JEV_WEIGHT * float(jev.probabilities.get(g, 0.0)) + (1 - BLEND_JEV_WEIGHT) * spread[g]
        for g in taxonomy.NAMES
    }
    top = max(blend, key=lambda g: blend[g])
    rule = "blend jev+deepseek" if deepseek.genre is not None else "jev only (deepseek unknown)"
    return Fused(top, round(blend[top], 4), rule)


__all__ = [
    "DEEPSEEK_MODEL",
    "DEEPSEEK_SURE",
    "JEV_MODEL",
    "KEY_NAMES",
    "OPENROUTER_KEY_ENV",
    "PROMPT_VERSION",
    "QUERY_VERSION",
    "SERPER_KEY_ENV",
    "SERPER_PRICE_ENV",
    "Answer",
    "DeepSeekProvider",
    "Fused",
    "GenreProvider",
    "JevProvider",
    "ProviderCallError",
    "ProviderUnavailable",
    "ResponseInvalid",
    "answer_distribution",
    "fuse",
    "missing_keys",
    "require_key",
    "search_query",
    "serper_usd_per_search",
    "web_search",
]
