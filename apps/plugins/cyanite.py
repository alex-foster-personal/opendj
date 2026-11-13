"""Official Cyanite REST wrapper for the optional ``opendj.cyanite`` provider (INTEL-13640).

Uses the user's ``CYANITE_API_KEY`` only; results map into :mod:`apps.genre_infer.claim_envelope`
and the V2 research wire shape. Rate-limit headers are kept on the provider quota record.

Never scrapes the web app, rotates free accounts, or treats five tracks as a similarity benchmark.

-Cursor-cloud
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from apps.genre_infer import providers as genre_providers
from apps.genre_infer.claim_envelope import (
    ClaimScope,
    EnrichmentClaim,
    SegmentScope,
    vendor_observation,
)
from apps.plugins.providers import (
    CYANITE_PLUGIN_ID,
    CYANITE_PROVENANCE_FIELDS,
    ProviderQuota,
    ProviderRegistry,
    quota_from_headers,
)

PLUGIN_ID = CYANITE_PLUGIN_ID
CYANITE_API_KEY_ENV = "CYANITE_API_KEY"
CYANITE_API_ROOT_ENV = "CYANITE_API_ROOT"
CYANITE_FIXTURE_ROOT_ENV = "CYANITE_FIXTURE_ROOT"

DEFAULT_API_ROOT = "https://rest-api.cyanite.ai"
API_VERSION_PREFIX = "/v1"

# Documented public facts (research doc, not live quotes).
FREE_WEB_MONTHLY_ANALYSES = 5
API_BASE_FEE_EUR_MONTH = 290
SIMILARITY_CATALOG_MIN_TRACKS = 1000

FORBIDDEN_TEST_PATHS: frozenset[str] = frozenset(
    {
        "webapp",
        "app.cyanite.ai",
        "account-rotation",
        "free-tier-rotation",
        "five-track-similarity-benchmark",
    }
)

MODEL_AUTO_TAG = "MainGenreV2"
MODEL_DESCRIPTION = "transformerCaption"
MODEL_SEGMENT = "SegmentationV1"


class CyaniteError(RuntimeError):
    """The Cyanite wrapper refused a call or could not parse a response."""


class CyaniteUnavailable(CyaniteError):
    """No user credential is configured; core paths must not treat this as fatal."""


class CyaniteForbiddenPath(CyaniteError):
    """A disallowed integration path (web app scrape, rotation, tiny similarity benchmark)."""


@dataclass(frozen=True)
class DocumentedFacts:
    free_web_monthly_analyses: int
    api_base_fee_eur_month: int
    similarity_catalog_min_tracks: int
    rate_limit_headers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "free_web_monthly_analyses": self.free_web_monthly_analyses,
            "api_base_fee_eur_month": self.api_base_fee_eur_month,
            "similarity_catalog_min_tracks": self.similarity_catalog_min_tracks,
            "rate_limit_headers": list(self.rate_limit_headers),
        }


def documented_facts() -> DocumentedFacts:
    return DocumentedFacts(
        free_web_monthly_analyses=FREE_WEB_MONTHLY_ANALYSES,
        api_base_fee_eur_month=API_BASE_FEE_EUR_MONTH,
        similarity_catalog_min_tracks=SIMILARITY_CATALOG_MIN_TRACKS,
        rate_limit_headers=(
            "x-ratelimit-limit",
            "x-ratelimit-remaining",
            "x-ratelimit-reset",
            "retry-after",
        ),
    )


def refuse_forbidden_test_path(path: str) -> None:
    lowered = path.casefold()
    for token in FORBIDDEN_TEST_PATHS:
        if token in lowered:
            raise CyaniteForbiddenPath(f"refusing forbidden Cyanite test path {path!r}")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: dict[str, Any]

    @property
    def quota(self) -> ProviderQuota:
        return quota_from_headers(self.headers)


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse: ...


@dataclass
class FixtureTransport:
    """Replay recorded HTTP exchanges from a directory (CI only, never live keys)."""

    root: Path
    _index: dict[tuple[str, str], dict[str, Any]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self._index = {}
        for path in sorted(self.root.glob("*.json")):
            if path.name == "README.json":
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            key = (raw["method"].upper(), raw["path"])
            self._index[key] = raw

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        _ = headers, body
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if path.startswith(API_VERSION_PREFIX):
            path = path[len(API_VERSION_PREFIX) :]
        key = (method.upper(), path)
        hit = self._index.get(key)
        if hit is None:
            raise CyaniteError(f"no recorded fixture for {method} {path}")
        payload = hit["body"]
        if not isinstance(payload, dict):
            raise CyaniteError(f"fixture body for {path} must be a JSON object")
        return HttpResponse(int(hit["status"]), dict(hit["headers"]), payload)


@dataclass
class UrllibTransport:
    timeout_s: float = 60.0

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                status = int(resp.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            hdrs = {k.lower(): v for k, v in exc.headers.items()}
            status = int(exc.code)
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise CyaniteError(f"{url} returned non-object JSON")
        return HttpResponse(status, hdrs, doc)


def default_transport() -> HttpTransport:
    fixture = os.environ.get(CYANITE_FIXTURE_ROOT_ENV, "").strip()
    if fixture:
        return FixtureTransport(Path(fixture))
    return UrllibTransport()


@dataclass
class CyaniteClient:
    """Thin official-API client; dependency injection carries fixtures or live HTTP."""

    api_key: str | None
    transport: HttpTransport
    api_root: str = DEFAULT_API_ROOT
    registry: ProviderRegistry | None = None
    plugin_id: str = PLUGIN_ID

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        registry: ProviderRegistry | None = None,
        transport: HttpTransport | None = None,
    ) -> CyaniteClient:
        env = environ if environ is not None else os.environ
        key = env.get(CYANITE_API_KEY_ENV, "").strip() or None
        root = env.get(CYANITE_API_ROOT_ENV, "").strip() or DEFAULT_API_ROOT
        return cls(
            api_key=key,
            transport=transport or default_transport(),
            api_root=root.rstrip("/"),
            registry=registry,
        )

    def configured(self) -> bool:
        if self.api_key:
            return True
        return bool(os.environ.get(CYANITE_FIXTURE_ROOT_ENV, "").strip())

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.api_root}{API_VERSION_PREFIX}{path}"

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            if os.environ.get(CYANITE_FIXTURE_ROOT_ENV, "").strip():
                return {
                    "x-api-key": "fixture",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            raise CyaniteUnavailable(f"{CYANITE_API_KEY_ENV} is not set")
        return {"x-api-key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"}

    def _call(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> HttpResponse:
        refuse_forbidden_test_path(path)
        url = self._url(path)
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        body = None if json_body is None else json.dumps(json_body).encode("utf-8")
        resp = self.transport.request(method, url, headers=self._headers(), body=body)
        if self.registry is not None:
            self.registry.record_quota(self.plugin_id, resp.headers)
        return resp

    def get_model_outputs(self, library_track_id: str, models: tuple[str, ...]) -> HttpResponse:
        query = [("model", m) for m in models]
        path = f"/library-tracks/{library_track_id}/models"
        url = self._url(path)
        url = f"{url}?{urllib.parse.urlencode(query)}"
        resp = self.transport.request("GET", url, headers=self._headers(), body=None)
        if self.registry is not None:
            self.registry.record_quota(self.plugin_id, resp.headers)
        return resp

    def prompt_search(self, query: str, *, limit: int = 20) -> HttpResponse:
        return self._call(
            "POST",
            "/library-tracks/prompt-search",
            query={"limit": str(limit)},
            json_body={"query": query},
        )

    def similar_tracks(self, library_track_id: str, *, limit: int = 20) -> HttpResponse:
        return self._call(
            "POST",
            f"/library-tracks/{library_track_id}/similar-tracks",
            query={"limit": str(limit)},
            json_body={},
        )


def v2_wire(claim: EnrichmentClaim) -> dict[str, Any]:
    """Map a stored enrichment claim to the V2 research JSON shape."""
    subject: dict[str, Any] = {"track_id": claim.stable_id}
    if claim.scope.kind == "segment" and claim.scope.segment is not None:
        subject["start_ms"] = claim.scope.segment.start_ms
        subject["end_ms"] = claim.scope.segment.end_ms
    confidence: dict[str, float] = {}
    if claim.confidence is not None:
        confidence["overall"] = claim.confidence
    return {
        "subject": subject,
        "concept": claim.dimension,
        "value": claim.value,
        "score": claim.confidence,
        "confidence": confidence,
        "source": {"provider": "cyanite", "model_version": claim.provider_model},
        "input_fingerprint": claim.input_fingerprint,
        "observed_at": claim.generated_at,
        "raw_evidence": claim.raw_evidence_ptr,
        "schema_version": claim.schema_version,
        "kind": claim.kind,
        "envelope_version": claim.envelope_version,
    }


def assert_v2_wire_complete(payload: Mapping[str, Any]) -> None:
    source = payload.get("source")
    if not isinstance(source, Mapping):
        raise CyaniteError("claim wire missing source object")
    if source.get("provider") != "cyanite":
        raise CyaniteError("claim wire source.provider must be cyanite")
    if not str(source.get("model_version", "")).strip():
        raise CyaniteError("claim wire missing source.model_version")
    for key in ("subject", "concept", "value", "input_fingerprint", "observed_at", "raw_evidence"):
        if key not in payload:
            raise CyaniteError(f"claim wire missing {key}")


def _raw_ptr(stable_id: str, model_version: str, suffix: str) -> str:
    return f"blob://cyanite/{stable_id}/{model_version}/{suffix}"


def claims_from_model_items(
    *,
    stable_id: str,
    input_fingerprint: str,
    items: list[Mapping[str, Any]],
    observed_at: str,
) -> list[EnrichmentClaim]:
    """Turn Cyanite ``items`` model outputs into enrichment claims."""
    out: list[EnrichmentClaim] = []
    for item in items:
        version = str(item.get("version", "")).strip()
        if not version:
            continue
        if version == MODEL_DESCRIPTION:
            text = item.get("caption") or item.get("text") or item.get("description")
            if not isinstance(text, str) or not text.strip():
                continue
            out.append(
                vendor_observation(
                    stable_id=stable_id,
                    dimension="description",
                    value=text.strip(),
                    source="cyanite",
                    provider_model=version,
                    schema_version=genre_providers.CYANITE_EXPLORER_SCHEMA,
                    input_fingerprint=input_fingerprint,
                    raw_evidence_ptr=_raw_ptr(stable_id, version, "description"),
                    generated=True,
                    generated_at=observed_at,
                )
            )
            continue
        if version == MODEL_SEGMENT:
            sections = item.get("sections")
            if not isinstance(sections, list):
                continue
            for idx, section in enumerate(sections):
                if not isinstance(section, Mapping):
                    continue
                start_s = section.get("startSeconds")
                end_s = section.get("endSeconds")
                label = section.get("label") or section.get("type") or f"section-{idx}"
                if not isinstance(start_s, int | float) or not isinstance(end_s, int | float):
                    continue
                scope = ClaimScope(
                    kind="segment",
                    segment=SegmentScope(int(start_s * 1000), int(end_s * 1000)),
                )
                out.append(
                    vendor_observation(
                        stable_id=stable_id,
                        dimension="segment",
                        value=str(label),
                        source="cyanite",
                        provider_model=version,
                        schema_version=genre_providers.CYANITE_EXPLORER_SCHEMA,
                        input_fingerprint=input_fingerprint,
                        raw_evidence_ptr=_raw_ptr(stable_id, version, f"segment-{idx}"),
                        scope=scope,
                        generated_at=observed_at,
                    )
                )
            continue
        tags = item.get("tags")
        if isinstance(tags, list) and tags:
            dimension = _dimension_for_model(version)
            for tag in tags:
                if not isinstance(tag, str) or not tag.strip():
                    continue
                scores = item.get("scores")
                confidence = None
                if isinstance(scores, Mapping) and isinstance(scores.get(tag), int | float):
                    confidence = float(scores[tag])
                out.append(
                    vendor_observation(
                        stable_id=stable_id,
                        dimension=dimension,
                        value=tag.strip(),
                        source="cyanite",
                        provider_model=version,
                        schema_version=genre_providers.CYANITE_EXPLORER_SCHEMA,
                        input_fingerprint=input_fingerprint,
                        raw_evidence_ptr=_raw_ptr(stable_id, version, tag.strip()),
                        confidence=confidence,
                        generated_at=observed_at,
                    )
                )
    return out


def claims_from_search_items(
    *,
    stable_id: str,
    input_fingerprint: str,
    items: list[Mapping[str, Any]],
    observed_at: str,
    search_kind: str,
) -> list[EnrichmentClaim]:
    out: list[EnrichmentClaim] = []
    for row in items:
        if not isinstance(row, Mapping):
            continue
        score = row.get("score")
        track = row.get("track")
        if not isinstance(track, Mapping):
            continue
        hit_id = track.get("id")
        if not isinstance(hit_id, str):
            continue
        conf = float(score) if isinstance(score, int | float) else None
        out.append(
            vendor_observation(
                stable_id=stable_id,
                dimension=search_kind,
                value={"library_track_id": hit_id, "title": track.get("title")},
                source="cyanite",
                provider_model=f"search/{search_kind}",
                schema_version=genre_providers.CYANITE_EXPLORER_SCHEMA,
                input_fingerprint=input_fingerprint,
                raw_evidence_ptr=_raw_ptr(stable_id, search_kind, hit_id),
                confidence=conf,
                generated_at=observed_at,
            )
        )
    return out


def _dimension_for_model(version: str) -> str:
    if version.startswith("MainGenre"):
        return "genre"
    if version.startswith("Mood"):
        return "mood"
    if version.startswith("Movement"):
        return "movement"
    if version.startswith("Instruments"):
        return "instrumentation"
    if version.startswith("Vocals"):
        return "vocals"
    if version.startswith("Bpm"):
        return "tempo_perceived"
    return "energy"


def enrich_from_fixtures(
    client: CyaniteClient,
    *,
    stable_id: str,
    library_track_id: str,
    input_fingerprint: str,
    observed_at: str,
) -> tuple[list[dict[str, Any]], ProviderQuota | None]:
    """Run all five recorded endpoint families and return V2 wire claims plus last quota."""
    models = client.get_model_outputs(library_track_id, (MODEL_AUTO_TAG, MODEL_DESCRIPTION, MODEL_SEGMENT))
    prompt = client.prompt_search("energetic electronic track")
    similar = client.similar_tracks(library_track_id)
    claims: list[EnrichmentClaim] = []
    claims.extend(
        claims_from_model_items(
            stable_id=stable_id,
            input_fingerprint=input_fingerprint,
            items=list(models.body.get("items", [])),
            observed_at=observed_at,
        )
    )
    claims.extend(
        claims_from_search_items(
            stable_id=stable_id,
            input_fingerprint=input_fingerprint,
            items=list(prompt.body.get("items", [])),
            observed_at=observed_at,
            search_kind="free_text",
        )
    )
    claims.extend(
        claims_from_search_items(
            stable_id=stable_id,
            input_fingerprint=input_fingerprint,
            items=list(similar.body.get("items", [])),
            observed_at=observed_at,
            search_kind="similarity",
        )
    )
    wire = [v2_wire(c) for c in claims]
    for w in wire:
        assert_v2_wire_complete(w)
    return wire, similar.quota


def cyanite_optional(registry: ProviderRegistry, environ: Mapping[str, str] | None = None) -> CyaniteClient | None:
    """Return a client when configured; never raises when the provider is absent."""
    client = CyaniteClient.from_environ(environ, registry=registry)
    return client if client.configured() else None


def record_response_quota(registry: ProviderRegistry, plugin_id: str, headers: Mapping[str, str]) -> ProviderQuota:
    return registry.record_quota(plugin_id, headers)


__all__ = [
    "API_BASE_FEE_EUR_MONTH",
    "CYANITE_API_KEY_ENV",
    "CYANITE_API_ROOT_ENV",
    "CYANITE_FIXTURE_ROOT_ENV",
    "CYANITE_PROVENANCE_FIELDS",
    "CyaniteClient",
    "CyaniteError",
    "CyaniteForbiddenPath",
    "CyaniteUnavailable",
    "DocumentedFacts",
    "FixtureTransport",
    "FORBIDDEN_TEST_PATHS",
    "FREE_WEB_MONTHLY_ANALYSES",
    "HttpResponse",
    "PLUGIN_ID",
    "SIMILARITY_CATALOG_MIN_TRACKS",
    "assert_v2_wire_complete",
    "claims_from_model_items",
    "claims_from_search_items",
    "cyanite_optional",
    "default_transport",
    "documented_facts",
    "enrich_from_fixtures",
    "record_response_quota",
    "refuse_forbidden_test_path",
    "v2_wire",
]
