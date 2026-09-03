"""Batched OpenRouter web-search resolution for low-confidence matches."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from .config import CFG

if TYPE_CHECKING:
    from .service import Escalation


class LlmMatchError(RuntimeError):
    """The LLM matcher is unconfigured or violates its wire contract."""


@dataclass(frozen=True)
class LlmDecision:
    source_id: str
    target_id: str | None
    confidence: float
    length_verified: bool
    evidence_urls: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise LlmMatchError("LLM decision source_id must not be empty")
        if self.target_id is not None and (
            not isinstance(self.target_id, str) or not self.target_id.strip()
        ):
            raise LlmMatchError("LLM decision target_id must be null or non-empty")
        if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool):
            raise LlmMatchError("LLM confidence must be numeric")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise LlmMatchError(
                f"LLM confidence must be finite and within 0..1: {self.confidence!r}"
            )
        object.__setattr__(self, "confidence", float(self.confidence))
        if not isinstance(self.length_verified, bool):
            raise LlmMatchError("LLM length_verified must be boolean")
        if (
            not isinstance(self.evidence_urls, tuple)
            or not self.evidence_urls
            or any(
                not isinstance(url, str) or not url.startswith(("https://", "http://"))
                for url in self.evidence_urls
            )
        ):
            raise LlmMatchError("LLM decision requires HTTP evidence URLs")


_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "target_id": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "length_verified": {"type": "boolean"},
                    "evidence_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                },
                "required": [
                    "source_id",
                    "target_id",
                    "confidence",
                    "length_verified",
                    "evidence_urls",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}


def build_batch_request(
    escalations: Sequence[Escalation],
    *,
    model: str,
) -> dict[str, Any]:
    if not escalations:
        raise LlmMatchError("cannot build an empty LLM match batch")
    if not model.strip():
        raise LlmMatchError("LLM model must be explicitly configured")
    batch = [row.as_prompt_dict() for row in escalations]
    return {
        "model": model.strip(),
        "messages": [
            {
                "role": "system",
                "content": (
                    "Resolve streaming-catalog candidates as the same recording or no "
                    "match. Use web search for every source. Track length verification "
                    "is mandatory before selecting a target. Never equate a remix, "
                    "live version, edit, or cover with the source recording."
                ),
            },
            {"role": "user", "content": json.dumps({"candidates": batch})},
        ],
        "tools": [{"type": "openrouter:web_search"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "streaming_track_matches",
                "strict": True,
                "schema": _DECISION_SCHEMA,
            },
        },
        "provider": {"require_parameters": True},
        "temperature": 0,
    }


def parse_batch_response(payload: object) -> tuple[LlmDecision, ...]:
    try:
        choices = payload["choices"]  # type: ignore[index]
        content = choices[0]["message"]["content"]
        decoded = json.loads(content)
        rows = decoded["decisions"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LlmMatchError("OpenRouter response does not contain decisions JSON") from exc
    if not isinstance(rows, list):
        raise LlmMatchError("OpenRouter decisions must be a list")
    decisions: list[LlmDecision] = []
    for row in rows:
        if not isinstance(row, dict):
            raise LlmMatchError("OpenRouter decision must be an object")
        try:
            decisions.append(
                LlmDecision(
                    source_id=row["source_id"],
                    target_id=row["target_id"],
                    confidence=row["confidence"],
                    length_verified=row["length_verified"],
                    evidence_urls=tuple(row["evidence_urls"]),
                )
            )
        except (KeyError, LlmMatchError, TypeError, ValueError) as exc:
            raise LlmMatchError(f"invalid OpenRouter decision: {row!r}") from exc
    return tuple(decisions)


class OpenRouterBatchMatcher:
    """One real OpenRouter request for all below-threshold candidates."""

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key.strip() or not model.strip():
            raise LlmMatchError("OpenRouter key and model must not be empty")
        self._api_key = api_key
        self._model = model

    @classmethod
    def from_env(cls) -> OpenRouterBatchMatcher:
        return cls(
            CFG.require_secret(CFG.openrouter_key_env),
            CFG.require_secret(CFG.openrouter_model_env),
        )

    def resolve(self, escalations: Sequence[Escalation]) -> tuple[LlmDecision, ...]:
        timeout = httpx.Timeout(
            connect=CFG.connect_timeout_s,
            read=CFG.read_timeout_s,
            write=30.0,
            pool=10.0,
        )
        response = httpx.post(
            f"{CFG.openrouter_api_base}/chat/completions",
            json=build_batch_request(escalations, model=self._model),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "HTTP-Referer": "https://github.com/former-work-account/music-dj-tools",
                "X-Title": "Open DJ",
            },
            timeout=timeout,
        )
        if response.status_code != 200:
            raise LlmMatchError(
                f"OpenRouter match batch returned HTTP {response.status_code}"
            )
        return parse_batch_response(response.json())
