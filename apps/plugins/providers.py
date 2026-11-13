"""Music-intelligence provider surface: capability kinds, credentials, quotas, provenance.

Provider secrets live in process memory or the named environment variable only.
They are never written to logs, track rows, library metadata, or plugin state.

-Cursor-cloud
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from apps.plugins.manifest import CAPABILITY_KINDS, PluginManifest, ProviderEntry

CYANITE_PLUGIN_ID = "opendj.cyanite"
CYANITE_PROVENANCE_FIELDS: tuple[str, ...] = ("provider", "model_version", "observed_at")
EPIDEMIC_PLUGIN_ID = "opendj.epidemic-sound"
EPIDEMIC_PROVENANCE_FIELDS: tuple[str, ...] = (
    "provider",
    "model_version",
    "observed_at",
    "catalog_track_id",
)

STABLE_API_KINDS: frozenset[str] = frozenset({"API", "MCP"})

# Parent issue #2493: Artlist Song Match and Musicbed Search by Song stay off until
# allowed-use review is recorded on the parent tracker.
PARENT_REVIEW_REQUIRED_IDS: frozenset[str] = frozenset(
    {
        "opendj.artlist",
        "opendj.musicbed",
    }
)

_REDACTED = "<redacted>"


class ProviderError(ValueError):
    """A provider rule the manifest or store must refuse."""


@dataclass(frozen=True)
class ProviderQuota:
    limit: int | None
    remaining: int | None
    reset_at: int | None
    retry_after_s: int | None

    def to_wire(self) -> dict[str, int | None]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_at": self.reset_at,
            "retry_after_s": self.retry_after_s,
        }


@dataclass(frozen=True)
class ProviderSurface:
    capability_kind: str
    store_surface: str
    credential_env: str | None
    credential_configured: bool
    terms_url: str | None
    provenance_fields: tuple[str, ...]
    quota: ProviderQuota | None

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "capability_kind": self.capability_kind,
            "store_surface": self.store_surface,
            "credential_env": self.credential_env,
            "credential_configured": self.credential_configured,
            "terms_url": self.terms_url,
            "provenance_fields": list(self.provenance_fields),
        }
        if self.quota is not None:
            out["quota"] = self.quota.to_wire()
        return out


def store_surface(capability_kind: str) -> str:
    """How the store labels access; trial and unavailable are never a stable API."""
    if capability_kind in STABLE_API_KINDS:
        return "stable-api"
    if capability_kind == "browser-only":
        return "browser-helper"
    if capability_kind == "public browse":
        return "public-browse"
    if capability_kind == "trial":
        return "trial"
    if capability_kind == "unavailable":
        return "unavailable"
    raise ProviderError(f"unknown capability kind {capability_kind!r}")


def refuse_advertised_stable_api(*, actual_kind: str, advertised_kind: str) -> None:
    """Refuse labeling browser-only or public browse providers as API/MCP/stable-api."""
    if actual_kind not in ("browser-only", "public browse"):
        return
    if advertised_kind in STABLE_API_KINDS or advertised_kind == "stable-api":
        raise ProviderError(f"refusing to advertise {actual_kind!r} as {advertised_kind!r}; use store_surface instead")


def require_parent_allowed_use_review(plugin_id: str, *, allowed_use_reviewed: bool) -> None:
    """Artlist and Musicbed stay disabled until parent #2493 records allowed-use review."""
    if plugin_id in PARENT_REVIEW_REQUIRED_IDS and not allowed_use_reviewed:
        raise ProviderError(f"refusing to enable {plugin_id!r} without parent allowed-use review (#2493)")


def _parse_int_header(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def quota_from_headers(headers: Mapping[str, str]) -> ProviderQuota:
    """Read rate-limit headers into a quota record; every named header is kept."""
    lowered = {k.lower(): v for k, v in headers.items()}
    return ProviderQuota(
        limit=_parse_int_header(lowered.get("x-ratelimit-limit")),
        remaining=_parse_int_header(lowered.get("x-ratelimit-remaining")),
        reset_at=_parse_int_header(lowered.get("x-ratelimit-reset")),
        retry_after_s=_parse_int_header(lowered.get("retry-after")),
    )


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, _REDACTED)
    return out


def safe_log_line(logger: logging.Logger, level: int, message: str, *secrets: str) -> None:
    """Log provider diagnostics with any configured secret values redacted."""
    logger.log(level, "%s", redact_secrets(message, secrets))


def refuse_secret_in_metadata(payload: Mapping[str, Any], secrets: Iterable[str]) -> None:
    """Refuse track or library rows that would persist a provider secret."""
    secret_set = {s for s in secrets if s}
    if not secret_set:
        return
    for key, value in payload.items():
        if isinstance(value, str) and value in secret_set:
            raise ProviderError(f"refusing to write provider secret into metadata field {key!r}")
        if isinstance(value, Mapping):
            refuse_secret_in_metadata(value, secret_set)


@dataclass
class ProviderRegistry:
    """Process-scoped credential and quota state for loaded provider plugins."""

    _credentials: dict[str, str] = field(default_factory=dict)
    _quotas: dict[str, ProviderQuota] = field(default_factory=dict)
    _entries: dict[str, ProviderEntry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def register(self, manifest: PluginManifest) -> None:
        entry = manifest.provider
        if entry is None:
            return
        with self._lock:
            self._entries[manifest.plugin_id] = entry
            if entry.credential_env:
                value = os.environ.get(entry.credential_env, "").strip()
                if value:
                    self._credentials[manifest.plugin_id] = value

    def set_credential(self, plugin_id: str, value: str) -> None:
        if not value.strip():
            raise ProviderError("refusing blank provider credential")
        with self._lock:
            self._entries.get(plugin_id)  # only registered providers
            self._credentials[plugin_id] = value.strip()

    def credential(self, plugin_id: str) -> str | None:
        with self._lock:
            return self._credentials.get(plugin_id)

    def record_quota(self, plugin_id: str, headers: Mapping[str, str]) -> ProviderQuota:
        quota = quota_from_headers(headers)
        with self._lock:
            self._quotas[plugin_id] = quota
        return quota

    def surface(self, plugin_id: str, entry: ProviderEntry | None = None) -> ProviderSurface | None:
        resolved = entry
        with self._lock:
            if resolved is None:
                resolved = self._entries.get(plugin_id)
            if resolved is None:
                return None
            configured = bool(
                resolved.credential_env
                and (
                    resolved.credential_env in os.environ
                    and os.environ.get(resolved.credential_env, "").strip()
                    or plugin_id in self._credentials
                )
            )
            quota = self._quotas.get(plugin_id)
        return ProviderSurface(
            capability_kind=resolved.capability_kind,
            store_surface=store_surface(resolved.capability_kind),
            credential_env=resolved.credential_env,
            credential_configured=configured,
            terms_url=resolved.terms_url,
            provenance_fields=resolved.provenance_fields,
            quota=quota,
        )

    def active_secrets(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._credentials.values())


__all__ = [
    "CAPABILITY_KINDS",
    "CYANITE_PLUGIN_ID",
    "CYANITE_PROVENANCE_FIELDS",
    "EPIDEMIC_PLUGIN_ID",
    "EPIDEMIC_PROVENANCE_FIELDS",
    "PARENT_REVIEW_REQUIRED_IDS",
    "ProviderError",
    "ProviderQuota",
    "ProviderRegistry",
    "ProviderSurface",
    "quota_from_headers",
    "redact_secrets",
    "refuse_advertised_stable_api",
    "refuse_secret_in_metadata",
    "require_parent_allowed_use_review",
    "safe_log_line",
    "store_surface",
]
