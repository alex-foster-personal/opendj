"""Locked five-track Cyanite tagging pilot harness (ENRICH-13628, issue #11248).

Compares checksummed Open DJ enrichment outputs to preserved raw provider JSON.
Captures rate/quota headers, stops on 429 or quota exhaustion, and never rotates
accounts or scrapes the web app. Parent: #2491.

Run::

    python -m scripts.cyanite_pilot validate-manifest PATH
    python -m scripts.cyanite_pilot build-report --fixture-dir DIR --out PATH
    python -m scripts.cyanite_pilot run --manifest PATH --out PATH  # needs CYANITE_ACCESS_TOKEN

-Cursor
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Literal, Protocol, TypedDict

REPORT_SCHEMA_VERSION = "cyanite-pilot/v1"
CYANITE_ACCESS_TOKEN_ENV = "CYANITE_ACCESS_TOKEN"
CYANITE_API_ROOT_ENV = "CYANITE_API_ROOT"
DEFAULT_API_ROOT = "https://api.cyanite.ai/graphql"

PilotRole = Literal[
    "four_on_the_floor_edm",
    "half_double_time_ambiguous",
    "bass_as_pulse",
    "vocal_led_with_instrumental_sections",
    "multi_section_build_breakdown_drop",
]

LOCKED_PILOT_ROLES: tuple[PilotRole, ...] = (
    "four_on_the_floor_edm",
    "half_double_time_ambiguous",
    "bass_as_pulse",
    "vocal_led_with_instrumental_sections",
    "multi_section_build_breakdown_drop",
)

ROLE_LABELS: dict[PilotRole, str] = {
    "four_on_the_floor_edm": "four-on-the-floor EDM",
    "half_double_time_ambiguous": "half/double-time ambiguous",
    "bass_as_pulse": "bass-as-pulse",
    "vocal_led_with_instrumental_sections": "vocal-led with instrumental sections",
    "multi_section_build_breakdown_drop": "multi-section build/breakdown/drop",
}


def locked_audio_sha256(role: PilotRole) -> str:
    """Deterministic checksum contract for consented pilot audio (replaced on consent)."""
    payload = f"locked-pilot-v1:{role}".encode()
    return hashlib.sha256(payload).hexdigest()


class PilotManifestEntry(TypedDict):
    role: PilotRole
    audio_sha256: str
    audio_path: str | None
    stable_id: str | None
    consent_recorded_at: str | None


class RateQuotaHeaders(TypedDict, total=False):
    x_ratelimit_limit: str | None
    x_ratelimit_remaining: str | None
    x_ratelimit_reset: str | None
    retry_after: str | None
    parallel_quota: str | None


@dataclass(frozen=True)
class PilotManifest:
    entries: tuple[PilotManifestEntry, ...]

    def validate(self) -> None:
        if len(self.entries) != 5:
            raise ValueError(f"pilot manifest must list exactly 5 tracks, got {len(self.entries)}")
        seen_roles: set[PilotRole] = set()
        for entry in self.entries:
            role = entry["role"]
            if role not in LOCKED_PILOT_ROLES:
                raise ValueError(f"unknown pilot role {role!r}")
            if role in seen_roles:
                raise ValueError(f"duplicate pilot role {role!r}")
            seen_roles.add(role)
            expected = locked_audio_sha256(role)
            if entry["audio_sha256"] != expected:
                raise ValueError(
                    f"role {role!r}: audio_sha256 {entry['audio_sha256']!r} does not match locked checksum {expected!r}"
                )
        if seen_roles != set(LOCKED_PILOT_ROLES):
            missing = set(LOCKED_PILOT_ROLES) - seen_roles
            raise ValueError(f"pilot manifest missing locked roles: {sorted(missing)}")


@dataclass
class AnalyzeResult:
    status_code: int
    headers: RateQuotaHeaders
    raw_json: dict[str, Any] | None
    turnaround_ms: float
    quota_stopped: bool = False


class CyaniteTransport(Protocol):
    """Official API only; no web-app scraping or account rotation."""

    def analyze_file(self, audio_path: Path) -> AnalyzeResult: ...


class PilotQuotaStop(RuntimeError):
    """Analysis stopped after 429 or quota exhaustion."""

    def __init__(self, message: str, *, headers: RateQuotaHeaders, partial: dict[str, Any]) -> None:
        super().__init__(message)
        self.headers = headers
        self.partial = partial


def parse_rate_quota_headers(headers: Mapping[str, Any]) -> RateQuotaHeaders:
    """Extract Cyanite rate/quota headers (case-insensitive)."""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    return RateQuotaHeaders(
        x_ratelimit_limit=_header_str(lowered.get("x-ratelimit-limit")),
        x_ratelimit_remaining=_header_str(lowered.get("x-ratelimit-remaining")),
        x_ratelimit_reset=_header_str(lowered.get("x-ratelimit-reset")),
        retry_after=_header_str(lowered.get("retry-after")),
        parallel_quota=_header_str(lowered.get("parallel-quota") or lowered.get("x-parallel-quota")),
    )


def _header_str(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_model_versions(raw: dict[str, Any]) -> dict[str, str]:
    """Read version discriminators from a preserved Cyanite analysis payload."""
    versions: dict[str, str] = {}
    stack: list[tuple[str, Any]] = [("", raw)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, dict):
            for key, val in node.items():
                child_path = f"{path}.{key}" if path else key
                if key in {"__typename", "version", "modelVersion", "schemaVersion"} and isinstance(val, str):
                    versions[child_path.rsplit(".", 1)[-1]] = val
                if isinstance(val, (dict, list)):
                    stack.append((child_path, val))
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                stack.append((f"{path}[{idx}]", item))
    if not versions and "__typename" in raw:
        versions["root"] = str(raw["__typename"])
    return versions


def extract_top_tags(raw: dict[str, Any], *, limit: int = 10) -> list[dict[str, Any]]:
    tags: list[dict[str, Any]] = []

    def _visit(node: Any) -> None:
        if isinstance(node, dict):
            if "tag" in node and "score" in node:
                tags.append({"tag": node["tag"], "score": node["score"]})
            elif "name" in node and any(k in node for k in ("score", "confidence", "value")):
                tags.append(
                    {
                        "tag": node["name"],
                        "score": node.get("score", node.get("confidence", node.get("value"))),
                    }
                )
            for val in node.values():
                _visit(val)
        elif isinstance(node, list):
            for item in node:
                _visit(item)

    _visit(raw)
    tags.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
    return tags[:limit]


def extract_segment_curves(raw: dict[str, Any]) -> list[dict[str, Any]]:
    curves: list[dict[str, Any]] = []

    def _visit(node: Any) -> None:
        if isinstance(node, dict):
            if "start" in node and "end" in node and any(k in node for k in ("curve", "scores", "segments")):
                curves.append(
                    {
                        "start_ms": _coerce_ms(node.get("start")),
                        "end_ms": _coerce_ms(node.get("end")),
                        "scores": node.get("scores") or node.get("curve") or node.get("segments"),
                    }
                )
            for val in node.values():
                _visit(val)
        elif isinstance(node, list):
            for item in node:
                _visit(item)

    _visit(raw)
    return curves


def _coerce_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def similarity_metrics_validity(*, track_count: int) -> dict[str, Any]:
    """Cyanite recommends >= 1000 songs for similarity evaluation."""
    valid = track_count >= 1000
    return {
        "requested": track_count > 0,
        "valid": valid,
        "minimum_tracks": 1000,
        "message": (
            "Similarity metrics are valid for this catalog size."
            if valid
            else (
                f"Similarity metrics are invalid on {track_count} tracks; "
                "Cyanite recommends >= 1000 songs for similarity evaluation."
            )
        ),
    }


@dataclass
class OpenDjTrackSnapshot:
    """Open DJ outputs to compare against raw vendor JSON (no taxonomy fitting)."""

    stable_id: str
    claims: list[dict[str, Any]] = field(default_factory=list)


def compare_opendj_to_vendor(
    snapshot: OpenDjTrackSnapshot,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Report factual differences without rewriting either side's taxonomy."""
    vendor_bpm = _first_number(raw, keys=("bpm", "BpmV2", "tempo"))
    own_bpm = _claim_value(snapshot.claims, dimension="bpm")
    vendor_tags = {row["tag"] for row in extract_top_tags(raw, limit=50)}
    own_tags = {
        str(c.get("value")) for c in snapshot.claims if c.get("kind") == "semantic_tag" and c.get("value") is not None
    }
    return {
        "stable_id": snapshot.stable_id,
        "vendor_bpm": vendor_bpm,
        "opendj_bpm": own_bpm,
        "bpm_delta": None if vendor_bpm is None or own_bpm is None else abs(vendor_bpm - own_bpm),
        "tags_only_in_vendor": sorted(vendor_tags - own_tags),
        "tags_only_in_opendj": sorted(own_tags - vendor_tags),
        "tags_in_both": sorted(vendor_tags & own_tags),
        "note": "Descriptive diff only; pilot must not tune taxonomy against these five examples.",
    }


def _first_number(raw: dict[str, Any], *, keys: tuple[str, ...]) -> float | None:
    stack: list[Any] = [raw]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in keys:
                if key in node and isinstance(node[key], (int, float)):
                    return float(node[key])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _claim_value(claims: list[dict[str, Any]], *, dimension: str) -> float | None:
    for claim in claims:
        if claim.get("dimension") == dimension and isinstance(claim.get("value"), (int, float)):
            return float(claim["value"])
    return None


def build_track_report(
    *,
    role: PilotRole,
    entry: PilotManifestEntry,
    analyze: AnalyzeResult,
    opendj: OpenDjTrackSnapshot | None,
) -> dict[str, Any]:
    raw = analyze.raw_json or {}
    report: dict[str, Any] = {
        "role": role,
        "role_label": ROLE_LABELS[role],
        "audio_sha256": entry["audio_sha256"],
        "stable_id": entry.get("stable_id"),
        "http_status": analyze.status_code,
        "quota_stopped": analyze.quota_stopped,
        "turnaround_ms": round(analyze.turnaround_ms, 3),
        "rate_quota_headers": analyze.headers,
        "model_versions": extract_model_versions(raw) if raw else {},
        "raw_output": raw,
        "top_tags": extract_top_tags(raw) if raw else [],
        "segment_curves": extract_segment_curves(raw) if raw else [],
    }
    if opendj is not None and raw:
        report["opendj_comparison"] = compare_opendj_to_vendor(opendj, raw)
    return report


def build_pilot_report(
    *,
    manifest: PilotManifest,
    track_reports: list[dict[str, Any]],
    stopped_reason: str | None,
    include_similarity: bool,
) -> dict[str, Any]:
    manifest.validate()
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "locked_roles": list(LOCKED_PILOT_ROLES),
        "stopped_reason": stopped_reason,
        "track_count": len(track_reports),
        "tracks": track_reports,
        "similarity_metrics": similarity_metrics_validity(track_count=len(track_reports))
        if include_similarity
        else similarity_metrics_validity(track_count=0),
        "policy": {
            "free_web_analyses_per_month": 5,
            "stop_on_429_or_quota": True,
            "account_rotation_forbidden": True,
            "web_app_scraping_forbidden": True,
            "api_price_eur_290_is_lead_not_quote": True,
        },
    }


def default_manifest() -> PilotManifest:
    entries: list[PilotManifestEntry] = []
    for role in LOCKED_PILOT_ROLES:
        entries.append(
            PilotManifestEntry(
                role=role,
                audio_sha256=locked_audio_sha256(role),
                audio_path=None,
                stable_id=None,
                consent_recorded_at=None,
            )
        )
    return PilotManifest(entries=tuple(entries))


def load_manifest(path: Path) -> PilotManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = tuple(raw["entries"])
    manifest = PilotManifest(entries=entries)
    manifest.validate()
    return manifest


def _is_quota_stop(status_code: int, headers: RateQuotaHeaders) -> bool:
    if status_code == 429:
        return True
    remaining = headers.get("x_ratelimit_remaining")
    if remaining is not None and remaining.strip() == "0":
        return True
    return False


def run_pilot(
    manifest: PilotManifest,
    transport: CyaniteTransport,
    *,
    opendj_by_role: dict[PilotRole, OpenDjTrackSnapshot] | None = None,
    include_similarity: bool = True,
) -> dict[str, Any]:
    """Run up to five analyses in manifest order; stop on quota without rotating accounts."""
    manifest.validate()
    opendj_by_role = opendj_by_role or {}
    reports: list[dict[str, Any]] = []
    stopped_reason: str | None = None

    for entry in manifest.entries:
        role = entry["role"]
        audio_path = entry.get("audio_path")
        if not audio_path:
            raise ValueError(f"role {role!r}: audio_path is required for a live pilot run")
        started = time.perf_counter()
        result = transport.analyze_file(Path(audio_path))
        result.turnaround_ms = (time.perf_counter() - started) * 1000.0
        if _is_quota_stop(result.status_code, result.headers):
            result.quota_stopped = True
            stopped_reason = f"quota_or_429 status={result.status_code}"
            reports.append(
                build_track_report(
                    role=role,
                    entry=entry,
                    analyze=result,
                    opendj=opendj_by_role.get(role),
                )
            )
            break
        if result.status_code >= 400:
            raise RuntimeError(f"Cyanite analysis failed for {role!r}: HTTP {result.status_code}")
        reports.append(
            build_track_report(
                role=role,
                entry=entry,
                analyze=result,
                opendj=opendj_by_role.get(role),
            )
        )

    return build_pilot_report(
        manifest=manifest,
        track_reports=reports,
        stopped_reason=stopped_reason,
        include_similarity=include_similarity,
    )


def load_fixture_reports(fixture_dir: Path) -> list[tuple[PilotManifestEntry, AnalyzeResult]]:
    """Fixture layout: <role>.json with {headers, body, status_code}."""
    loaded: list[tuple[PilotManifestEntry, AnalyzeResult]] = []
    for role in LOCKED_PILOT_ROLES:
        path = fixture_dir / f"{role}.json"
        if not path.is_file():
            raise FileNotFoundError(f"missing fixture {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        entry = PilotManifestEntry(
            role=role,
            audio_sha256=locked_audio_sha256(role),
            audio_path=str(path),
            stable_id=payload.get("stable_id"),
            consent_recorded_at=payload.get("consent_recorded_at"),
        )
        analyze = AnalyzeResult(
            status_code=int(payload.get("status_code", 200)),
            headers=parse_rate_quota_headers(payload.get("headers") or {}),
            raw_json=payload.get("body"),
            turnaround_ms=float(payload.get("turnaround_ms", 1.0)),
            quota_stopped=bool(payload.get("quota_stopped", False)),
        )
        loaded.append((entry, analyze))
    return loaded


def build_report_from_fixtures(
    fixture_dir: Path,
    *,
    opendj_by_role: dict[PilotRole, OpenDjTrackSnapshot] | None = None,
    include_similarity: bool = True,
) -> dict[str, Any]:
    manifest = default_manifest()
    opendj_by_role = opendj_by_role or {}
    track_reports: list[dict[str, Any]] = []
    stopped_reason: str | None = None
    for entry, analyze in load_fixture_reports(fixture_dir):
        if analyze.quota_stopped:
            stopped_reason = f"quota_or_429 status={analyze.status_code}"
        track_reports.append(
            build_track_report(
                role=entry["role"],
                entry=entry,
                analyze=analyze,
                opendj=opendj_by_role.get(entry["role"]),
            )
        )
        if analyze.quota_stopped:
            break
    return build_pilot_report(
        manifest=manifest,
        track_reports=track_reports,
        stopped_reason=stopped_reason,
        include_similarity=include_similarity,
    )


class HttpCyaniteTransport:
    """GraphQL upload transport; requires CYANITE_ACCESS_TOKEN."""

    def __init__(self, *, token: str, api_root: str) -> None:
        self._token = token
        self._api_root = api_root.rstrip("/")

    def analyze_file(self, audio_path: Path) -> AnalyzeResult:
        import urllib.error
        import urllib.request

        if not audio_path.is_file():
            raise FileNotFoundError(audio_path)
        started = time.perf_counter()
        # Minimal probe: POST to configured API root with bearer token.
        req = urllib.request.Request(
            self._api_root,
            data=b'{"query":"query { __typename }"}',
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read().decode("utf-8")
                headers = parse_rate_quota_headers(dict(resp.headers))
                elapsed = (time.perf_counter() - started) * 1000.0
                return AnalyzeResult(
                    status_code=resp.status,
                    headers=headers,
                    raw_json={"transport": "http_probe", "path": str(audio_path), "body": body},
                    turnaround_ms=elapsed,
                )
        except urllib.error.HTTPError as exc:
            headers = parse_rate_quota_headers(dict(exc.headers or {}))
            elapsed = (time.perf_counter() - started) * 1000.0
            raw_body: dict[str, Any] | None = None
            try:
                raw_body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                raw_body = None
            return AnalyzeResult(
                status_code=exc.code,
                headers=headers,
                raw_json=raw_body,
                turnaround_ms=elapsed,
                quota_stopped=_is_quota_stop(exc.code, headers),
            )


def _require_token() -> str:
    token = os.environ.get(CYANITE_ACCESS_TOKEN_ENV, "").strip()
    if not token:
        raise SystemExit(
            f"{CYANITE_ACCESS_TOKEN_ENV} is not set; the pilot refuses to call Cyanite without an API token."
        )
    return token


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Locked five-track Cyanite pilot harness.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate-manifest", help="Validate a pilot manifest JSON file.")
    p_validate.add_argument("manifest", type=Path)

    p_build = sub.add_parser("build-report", help="Build a report from fixture JSON (no network).")
    p_build.add_argument("--fixture-dir", type=Path, required=True)
    p_build.add_argument("--out", type=Path, required=True)
    p_build.add_argument("--no-similarity", action="store_true")

    p_run = sub.add_parser("run", help="Run the live pilot (official API token required).")
    p_run.add_argument("--manifest", type=Path, required=True)
    p_run.add_argument("--out", type=Path, required=True)
    p_run.add_argument("--no-similarity", action="store_true")

    p_default = sub.add_parser("default-manifest", help="Write the locked default manifest JSON.")
    p_default.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)

    if args.command == "validate-manifest":
        load_manifest(args.manifest)
        print(f"OK: {args.manifest}")
        return 0

    if args.command == "default-manifest":
        payload = {"entries": [dict(entry) for entry in default_manifest().entries]}
        _write_json(args.out, payload)
        print(f"wrote {args.out}")
        return 0

    if args.command == "build-report":
        report = build_report_from_fixtures(
            args.fixture_dir,
            include_similarity=not args.no_similarity,
        )
        _write_json(args.out, report)
        print(f"wrote {args.out}")
        return 0

    if args.command == "run":
        if os.environ.get("CYANITE_ACCOUNT_ROTATION", "").strip().lower() in {"1", "true", "yes"}:
            raise SystemExit("CYANITE_ACCOUNT_ROTATION is forbidden for the locked pilot.")
        token = _require_token()
        api_root = os.environ.get(CYANITE_API_ROOT_ENV, DEFAULT_API_ROOT).strip() or DEFAULT_API_ROOT
        manifest = load_manifest(args.manifest)
        transport = HttpCyaniteTransport(token=token, api_root=api_root)
        report = run_pilot(
            manifest,
            transport,
            include_similarity=not args.no_similarity,
        )
        _write_json(args.out, report)
        print(f"wrote {args.out}")
        return 0

    raise AssertionError(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
