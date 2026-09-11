"""Batched OpenRouter web-search resolution for low-confidence matches."""

from __future__ import annotations

import hashlib
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
    evidence_urls: tuple[str, ...]
    web_search_batch_id: str
    web_search_requests: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise LlmMatchError("LLM decision source_id must not be empty")
        if self.target_id is not None and (
            not isinstance(self.target_id, str) or not self.target_id.strip()
        ):
            raise LlmMatchError("LLM decision target_id must be null or non-empty")
        if (
            not isinstance(self.confidence, (int, float))
            or isinstance(self.confidence, bool)
            or not math.isfinite(self.confidence)
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise LlmMatchError(f"invalid LLM confidence: {self.confidence!r}")
        object.__setattr__(self, "confidence", float(self.confidence))
        if (
            not self.evidence_urls
            or any(
                not isinstance(url, str) or not url.startswith(("https://", "http://"))
                for url in self.evidence_urls
            )
        ):
            raise LlmMatchError("LLM decision requires HTTP evidence URLs")
        if len(self.web_search_batch_id) != 64:
            raise LlmMatchError("LLM decision requires a SHA-256 web-search batch id")
        if (
            not isinstance(self.web_search_requests, int)
            or isinstance(self.web_search_requests, bool)
            or self.web_search_requests < 1
        ):
            raise LlmMatchError("LLM decision requires a positive web-search request count")


_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "batch_id": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "target_id": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                },
                "required": ["source_id", "target_id", "confidence", "evidence_urls"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["batch_id", "decisions"],
    "additionalProperties": False,
}


def batch_id_for(escalations: Sequence[Escalation]) -> str:
    batch = [row.as_prompt_dict() for row in escalations]
    canonical = json.dumps(batch, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_batch_request(escalations: Sequence[Escalation], *, model: str) -> dict[str, Any]:
    if not escalations:
        raise LlmMatchError("cannot build an empty LLM match batch")
    if not model.strip():
        raise LlmMatchError("LLM model must be explicitly configured")
    batch = [row.as_prompt_dict() for row in escalations]
    batch_id = batch_id_for(escalations)
    return {
        "model": model.strip(),
        "messages": [
            {
                "role": "system",
                "content": (
                    "Resolve streaming-catalog candidates as the same recording or no "
                    "match. Use web search for every source and copy cited URLs exactly "
                    "into evidence_urls. Echo batch_id exactly. Never equate a remix, "
                    "live version, edit, or cover with the source recording. The caller "
                    "will verify track lengths deterministically."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({"batch_id": batch_id, "candidates": batch}),
            },
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


def parse_batch_response(payload: object, *, expected_batch_id: str) -> tuple[LlmDecision, ...]:
    try:
        choices = payload["choices"]  # type: ignore[index]
        message = choices[0]["message"]
        content = message["content"]
        annotations = message["annotations"]
        web_search_requests = payload["usage"]["server_tool_use"]["web_search_requests"]  # type: ignore[index]
        decoded = json.loads(content)
        response_batch_id = decoded["batch_id"]
        rows = decoded["decisions"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LlmMatchError(
            "OpenRouter response lacks decisions, citations, or web_search_requests"
        ) from exc
    if (
        not isinstance(web_search_requests, int)
        or isinstance(web_search_requests, bool)
        or web_search_requests < 1
    ):
        raise LlmMatchError("OpenRouter web_search_requests must be positive")
    if response_batch_id != expected_batch_id:
        raise LlmMatchError("OpenRouter response batch_id does not match the request")
    if not isinstance(annotations, list):
        raise LlmMatchError("OpenRouter response annotations must be a list")
    citation_urls = {
        annotation.get("url_citation", {}).get("url")
        for annotation in annotations
        if isinstance(annotation, dict) and annotation.get("type") == "url_citation"
    }
    citation_urls = {
        url
        for url in citation_urls
        if isinstance(url, str) and url.startswith(("http://", "https://"))
    }
    if not citation_urls:
        raise LlmMatchError("OpenRouter response contains no URL citation evidence")
    if not isinstance(rows, list):
        raise LlmMatchError("OpenRouter decisions must be a list")
    decisions: list[LlmDecision] = []
    for row in rows:
        if not isinstance(row, dict):
            raise LlmMatchError("OpenRouter decision must be an object")
        expected_fields = {"source_id", "target_id", "confidence", "evidence_urls"}
        if set(row) != expected_fields or not isinstance(row["evidence_urls"], list):
            raise LlmMatchError("OpenRouter decision fields violate the strict schema")
        evidence_urls = tuple(row["evidence_urls"])
        if not evidence_urls or not set(evidence_urls) <= citation_urls:
            raise LlmMatchError("decision evidence is not proven by URL citations")
        decisions.append(
            LlmDecision(
                source_id=row["source_id"],
                target_id=row["target_id"],
                confidence=row["confidence"],
                evidence_urls=evidence_urls,
                web_search_batch_id=expected_batch_id,
                web_search_requests=web_search_requests,
            )
        )
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
            connect=CFG.connect_timeout_s, read=CFG.read_timeout_s, write=30.0, pool=10.0
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
            raise LlmMatchError(f"OpenRouter match batch returned HTTP {response.status_code}")
        return parse_batch_response(
            response.json(),
            expected_batch_id=batch_id_for(escalations),
        )
