"""Official Epidemic Sound partner API and MCP wrapper (INTEL-13645, parent #2493).

Uses only ``https://api.epidemicsound.com``, legacy partner ``partner-content-api.epidemicsound.com``
(v0), and the published MCP endpoint. Catalog hits are claim-envelope observations labeled as
Epidemic catalog items, never as tracks in the user's owned library.

-Cursor-cloud
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from apps.genre_infer.claim_envelope import (
    ClaimScope,
    EnrichmentClaim,
    SegmentScope,
    vendor_observation,
)
from apps.plugins.providers import (
    EPIDEMIC_PLUGIN_ID,
    EPIDEMIC_PROVENANCE_FIELDS,
    ProviderQuota,
    ProviderRegistry,
    quota_from_headers,
    refuse_secret_in_metadata,
    safe_log_line,
)

PLUGIN_ID = EPIDEMIC_PLUGIN_ID
EPIDEMIC_API_KEY_ENV = "EPIDEMIC_SOUND_API_KEY"
EPIDEMIC_MCP_KEY_ENV = "EPIDEMIC_SOUND_MCP_API_KEY"
EPIDEMIC_FIXTURE_ROOT_ENV = "EPIDEMIC_SOUND_FIXTURE_ROOT"

DEFAULT_API_ROOT = "https://api.epidemicsound.com"
LEGACY_API_ROOT = "https://partner-content-api.epidemicsound.com"
MCP_SERVICE_URL = "https://www.epidemicsound.com/a/mcp-service/mcp"
OFFICIAL_DOCS_URL = "https://developers.epidemicsound.com/"
API_PREFIX = "/v0"

ALLOWED_API_HOSTS: frozenset[str] = frozenset(
    {
        "api.epidemicsound.com",
        "partner-content-api.epidemicsound.com",
    }
)

SCHEMA_PARTNER_V0 = "epidemic-partner-api/v0"
SCHEMA_MCP_BETA = "epidemic-mcp/beta"


class EpidemicError(RuntimeError):
    """The Epidemic wrapper refused a call or could not parse a response."""


class EpidemicUnavailable(EpidemicError):
    """No user credential and no fixture root; optional provider must not block core."""


class EpidemicForbiddenUrl(EpidemicError):
    """A URL outside the official partner API or MCP was requested."""


def assert_allowed_epidemic_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").casefold()
    if host in ALLOWED_API_HOSTS and parsed.scheme == "https":
        return
    if host == "www.epidemicsound.com" and parsed.scheme == "https":
        if parsed.path.startswith("/a/mcp-service/"):
            return
        raise EpidemicForbiddenUrl(f"refusing consumer Epidemic URL {url!r}")
    raise EpidemicForbiddenUrl(f"refusing non-official Epidemic URL {url!r}")


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
    """Replay recorded partner-API and MCP exchanges (CI only)."""

    root: Path
    _api_index: dict[tuple[str, str], dict[str, Any]] = field(init=False, repr=False)
    _mcp_index: dict[str, dict[str, Any]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self._api_index = {}
        self._mcp_index = {}
        api_dir = self.root / "partner-api"
        mcp_dir = self.root / "mcp"
        if api_dir.is_dir():
            for path in sorted(api_dir.glob("*.json")):
                raw = json.loads(path.read_text(encoding="utf-8"))
                key = (raw["method"].upper(), raw["path"])
                self._api_index[key] = raw
        if mcp_dir.is_dir():
            for path in sorted(mcp_dir.glob("*.json")):
                raw = json.loads(path.read_text(encoding="utf-8"))
                tool = raw["tool"]
                self._mcp_index[str(tool)] = raw

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> HttpResponse:
        _ = headers
        assert_allowed_epidemic_url(url)
        parsed = urllib.parse.urlparse(url)
        if parsed.path.startswith("/a/mcp-service/"):
            doc = json.loads(body or b"{}")
            params = doc.get("params") if isinstance(doc, dict) else None
            tool = params.get("name") if isinstance(params, dict) else None
            if not isinstance(tool, str):
                raise EpidemicError("MCP fixture call missing tool name")
            hit = self._mcp_index.get(tool)
            if hit is None:
                raise EpidemicError(f"no recorded MCP fixture for tool {tool!r}")
            payload = hit["result"]
            if not isinstance(payload, dict):
                raise EpidemicError(f"MCP fixture result for {tool} must be an object")
            return HttpResponse(int(hit.get("status", 200)), dict(hit.get("headers", {})), payload)
        path = parsed.path
        if path.startswith(API_PREFIX):
            path = path[len(API_PREFIX) :]
        key = (method.upper(), path)
        hit = self._api_index.get(key)
        if hit is None:
            raise EpidemicError(f"no recorded fixture for {method} {path}")
        payload = hit["body"]
        if not isinstance(payload, dict):
            raise EpidemicError(f"fixture body for {path} must be a JSON object")
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
        assert_allowed_epidemic_url(url)
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
            raise EpidemicError(f"{url} returned non-object JSON")
        return HttpResponse(status, hdrs, doc)


def default_transport() -> HttpTransport:
    fixture = os.environ.get(EPIDEMIC_FIXTURE_ROOT_ENV, "").strip()
    if fixture:
        return FixtureTransport(Path(fixture))
    return UrllibTransport()


def _catalog_value(track_id: str, title: str | None, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "catalog_track_id": track_id,
        "title": title,
        "owned_library": False,
        "catalog_provider": "epidemic-sound",
    }
    if extra:
        out.update(extra)
    return out


def v2_wire(claim: EnrichmentClaim) -> dict[str, Any]:
    subject: dict[str, Any] = {"track_id": claim.stable_id}
    if claim.scope.kind == "segment" and claim.scope.segment is not None:
        subject["start_ms"] = claim.scope.segment.start_ms
        subject["end_ms"] = claim.scope.segment.end_ms
    catalog_identity = None
    if isinstance(claim.value, dict):
        cid = claim.value.get("catalog_track_id")
        if isinstance(cid, str) and cid:
            catalog_identity = {
                "provider": "epidemic-sound",
                "catalog_track_id": cid,
                "owned_library": False,
            }
    wire: dict[str, Any] = {
        "subject": subject,
        "concept": claim.dimension,
        "value": claim.value,
        "score": claim.confidence,
        "confidence": {"overall": claim.confidence} if claim.confidence is not None else {},
        "source": {
            "provider": "epidemic-sound",
            "model_version": claim.provider_model,
        },
        "input_fingerprint": claim.input_fingerprint,
        "observed_at": claim.generated_at,
        "raw_evidence": claim.raw_evidence_ptr,
        "schema_version": claim.schema_version,
        "kind": claim.kind,
        "envelope_version": claim.envelope_version,
    }
    if catalog_identity is not None:
        wire["catalog_identity"] = catalog_identity
    return wire


def assert_catalog_claim_wire_complete(payload: Mapping[str, Any]) -> None:
    source = payload.get("source")
    if not isinstance(source, Mapping) or source.get("provider") != "epidemic-sound":
        raise EpidemicError("claim wire missing source.provider epidemic-sound")
    if not str(source.get("model_version", "")).strip():
        raise EpidemicError("claim wire missing source.model_version")
    identity = payload.get("catalog_identity")
    if not isinstance(identity, Mapping):
        raise EpidemicError("catalog hit missing catalog_identity")
    if identity.get("owned_library") is not False:
        raise EpidemicError("catalog hit must not be marked as owned library")
    if not str(identity.get("catalog_track_id", "")).strip():
        raise EpidemicError("catalog hit missing catalog_track_id")
    for key in ("subject", "concept", "value", "input_fingerprint", "observed_at", "raw_evidence"):
        if key not in payload:
            raise EpidemicError(f"claim wire missing {key}")


def _raw_ptr(kind: str, catalog_track_id: str, suffix: str) -> str:
    return f"blob://epidemic-sound/{kind}/{catalog_track_id}/{suffix}"


@dataclass
class EpidemicClient:
    api_key: str | None
    mcp_key: str | None
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
    ) -> EpidemicClient:
        env = environ if environ is not None else os.environ
        api_key = env.get(EPIDEMIC_API_KEY_ENV, "").strip() or None
        mcp_key = env.get(EPIDEMIC_MCP_KEY_ENV, "").strip() or None
        return cls(
            api_key=api_key,
            mcp_key=mcp_key,
            transport=transport or default_transport(),
            registry=registry,
        )

    def configured(self) -> bool:
        if os.environ.get(EPIDEMIC_FIXTURE_ROOT_ENV, "").strip():
            return True
        return bool(self.api_key or self.mcp_key)

    def _api_url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.api_root}{API_PREFIX}{path}"
        assert_allowed_epidemic_url(url)
        return url

    def _api_headers(self) -> dict[str, str]:
        if not self.api_key:
            if os.environ.get(EPIDEMIC_FIXTURE_ROOT_ENV, "").strip():
                return {"x-api-key": "fixture", "Accept": "application/json", "Content-Type": "application/json"}
            raise EpidemicUnavailable(f"{EPIDEMIC_API_KEY_ENV} is not set")
        return {"x-api-key": self.api_key, "Accept": "application/json", "Content-Type": "application/json"}

    def _record_quota(self, headers: Mapping[str, str]) -> None:
        if self.registry is not None:
            self.registry.record_quota(self.plugin_id, headers)

    def _api_call(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> HttpResponse:
        url = self._api_url(path)
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        body = None if json_body is None else json.dumps(json_body).encode("utf-8")
        resp = self.transport.request(method, url, headers=self._api_headers(), body=body)
        self._record_quota(resp.headers)
        return resp

    def _mcp_call(self, tool: str, arguments: Mapping[str, Any]) -> HttpResponse:
        if not self.mcp_key:
            if os.environ.get(EPIDEMIC_FIXTURE_ROOT_ENV, "").strip():
                token = "fixture"
            else:
                raise EpidemicUnavailable(f"{EPIDEMIC_MCP_KEY_ENV} is not set")
        else:
            token = self.mcp_key
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": dict(arguments)},
        }
        resp = self.transport.request(
            "POST",
            MCP_SERVICE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            body=json.dumps(payload).encode("utf-8"),
        )
        self._record_quota(resp.headers)
        return resp


def claims_from_track_rows(
    *,
    stable_id: str,
    input_fingerprint: str,
    rows: list[Mapping[str, Any]],
    observed_at: str,
    dimension: str,
    model_version: str,
) -> list[EnrichmentClaim]:
    out: list[EnrichmentClaim] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        track_id = row.get("id") or row.get("trackId")
        if not isinstance(track_id, str):
            continue
        title = row.get("title") if isinstance(row.get("title"), str) else None
        score = row.get("score")
        conf = float(score) if isinstance(score, int | float) else None
        out.append(
            vendor_observation(
                stable_id=stable_id,
                dimension=dimension,
                value=_catalog_value(track_id, title),
                source="epidemic-sound",
                provider_model=model_version,
                schema_version=SCHEMA_PARTNER_V0,
                input_fingerprint=input_fingerprint,
                raw_evidence_ptr=_raw_ptr(dimension, track_id, "hit"),
                confidence=conf,
                generated_at=observed_at,
            )
        )
    return out


def claims_from_highlights(
    *,
    stable_id: str,
    catalog_track_id: str,
    input_fingerprint: str,
    highlights: list[Mapping[str, Any]],
    observed_at: str,
) -> list[EnrichmentClaim]:
    out: list[EnrichmentClaim] = []
    for idx, item in enumerate(highlights):
        if not isinstance(item, Mapping):
            continue
        start_ms = item.get("startMs")
        end_ms = item.get("endMs")
        if not isinstance(start_ms, int) or not isinstance(end_ms, int):
            continue
        label = item.get("label") or item.get("type") or f"highlight-{idx}"
        scope = ClaimScope(kind="segment", segment=SegmentScope(start_ms, end_ms))
        out.append(
            vendor_observation(
                stable_id=stable_id,
                dimension="highlight",
                value=_catalog_value(catalog_track_id, None, {"label": label, "highlight_index": idx}),
                source="epidemic-sound",
                provider_model="partner-api/highlights",
                schema_version=SCHEMA_PARTNER_V0,
                input_fingerprint=input_fingerprint,
                raw_evidence_ptr=_raw_ptr("highlight", catalog_track_id, str(idx)),
                scope=scope,
                generated_at=observed_at,
            )
        )
    return out


def claims_from_edit_recording(
    *,
    stable_id: str,
    catalog_track_id: str,
    input_fingerprint: str,
    result: Mapping[str, Any],
    observed_at: str,
) -> list[EnrichmentClaim]:
    job_id = result.get("jobId")
    edit_id = result.get("editId")
    duration_ms = result.get("targetDurationMs")
    if not isinstance(job_id, str) or not isinstance(edit_id, str):
        raise EpidemicError("MCP EditRecording result missing jobId or editId")
    scope = None
    if isinstance(duration_ms, int) and duration_ms > 0:
        scope = ClaimScope(kind="segment", segment=SegmentScope(0, duration_ms))
    out = [
        vendor_observation(
            stable_id=stable_id,
            dimension="loop",
            value=_catalog_value(catalog_track_id, None, {"edit_id": edit_id, "job_id": job_id}),
            source="epidemic-sound",
            provider_model="mcp/EditRecording",
            schema_version=SCHEMA_MCP_BETA,
            input_fingerprint=input_fingerprint,
            raw_evidence_ptr=_raw_ptr("loop", catalog_track_id, edit_id),
            scope=scope or ClaimScope(kind="track"),
            generated_at=observed_at,
        ),
        vendor_observation(
            stable_id=stable_id,
            dimension="version",
            value=_catalog_value(catalog_track_id, None, {"edit_id": edit_id, "variant": "edit"}),
            source="epidemic-sound",
            provider_model="mcp/EditRecording",
            schema_version=SCHEMA_MCP_BETA,
            input_fingerprint=input_fingerprint,
            raw_evidence_ptr=_raw_ptr("version", catalog_track_id, edit_id),
            scope=scope or ClaimScope(kind="track"),
            generated_at=observed_at,
        ),
    ]
    return out


def enrich_from_fixtures(
    client: EpidemicClient,
    *,
    stable_id: str,
    reference_track_id: str,
    input_fingerprint: str,
    observed_at: str,
) -> tuple[list[dict[str, Any]], ProviderQuota | None]:
    """Run partner-API and MCP fixture families; return V2 wire claims with catalog provenance."""
    semantic = client._api_call("POST", "/tracks/search", json_body={"query": "driving electronic"})
    similar = client._api_call("GET", f"/tracks/{reference_track_id}/similar-sections")
    highlights = client._api_call("GET", f"/tracks/{reference_track_id}/highlights")
    edit = client._mcp_call(
        "EditRecording",
        {"recordingId": reference_track_id, "targetDurationMs": 47000, "requiredRegionsAtOffsets": []},
    )
    claims: list[EnrichmentClaim] = []
    claims.extend(
        claims_from_track_rows(
            stable_id=stable_id,
            input_fingerprint=input_fingerprint,
            rows=list(semantic.body.get("tracks", semantic.body.get("items", []))),
            observed_at=observed_at,
            dimension="semantic_search",
            model_version="partner-api/tracks/search",
        )
    )
    claims.extend(
        claims_from_track_rows(
            stable_id=stable_id,
            input_fingerprint=input_fingerprint,
            rows=list(similar.body.get("tracks", similar.body.get("items", []))),
            observed_at=observed_at,
            dimension="similarity",
            model_version="partner-api/similar-sections",
        )
    )
    claims.extend(
        claims_from_highlights(
            stable_id=stable_id,
            catalog_track_id=reference_track_id,
            input_fingerprint=input_fingerprint,
            highlights=list(highlights.body.get("highlights", [])),
            observed_at=observed_at,
        )
    )
    claims.extend(
        claims_from_edit_recording(
            stable_id=stable_id,
            catalog_track_id=reference_track_id,
            input_fingerprint=input_fingerprint,
            result=edit.body,
            observed_at=observed_at,
        )
    )
    wire = [v2_wire(c) for c in claims]
    for item in wire:
        assert_catalog_claim_wire_complete(item)
    return wire, edit.quota


def epidemic_optional(registry: ProviderRegistry, environ: Mapping[str, str] | None = None) -> EpidemicClient | None:
    client = EpidemicClient.from_environ(environ, registry=registry)
    return client if client.configured() else None


def safe_provider_log(logger: Any, level: int, message: str, client: EpidemicClient) -> None:
    """Log a provider line with any configured Epidemic credentials redacted."""
    secrets = tuple(s for s in (client.api_key, client.mcp_key) if s)
    safe_log_line(logger, level, message, *secrets)


def refuse_credentials_in_metadata(payload: Mapping[str, Any], client: EpidemicClient) -> None:
    secrets = tuple(s for s in (client.api_key, client.mcp_key) if s)
    refuse_secret_in_metadata(payload, secrets)


__all__ = [
    "ALLOWED_API_HOSTS",
    "EPIDEMIC_API_KEY_ENV",
    "EPIDEMIC_FIXTURE_ROOT_ENV",
    "EPIDEMIC_MCP_KEY_ENV",
    "EPIDEMIC_PROVENANCE_FIELDS",
    "EpidemicClient",
    "EpidemicError",
    "EpidemicForbiddenUrl",
    "EpidemicUnavailable",
    "FixtureTransport",
    "MCP_SERVICE_URL",
    "OFFICIAL_DOCS_URL",
    "PLUGIN_ID",
    "assert_allowed_epidemic_url",
    "assert_catalog_claim_wire_complete",
    "claims_from_edit_recording",
    "claims_from_highlights",
    "claims_from_track_rows",
    "default_transport",
    "enrich_from_fixtures",
    "epidemic_optional",
    "refuse_credentials_in_metadata",
    "safe_provider_log",
    "v2_wire",
]
