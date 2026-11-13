"""Schema and min_app_version gate for a skin package (PLAN.md section 3.5)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from apps.shared.semver import Semver, parse_semver


class SkinVersionError(ValueError):
    """A skin manifest's version keys refuse it."""


@dataclass(frozen=True)
class SkinVersion:
    schema: int
    version: Semver
    min_app_version: Semver


def _semver_key(manifest: Mapping[str, Any], key: str) -> Semver:
    raw = manifest.get(key)
    if not isinstance(raw, str):
        raise SkinVersionError(f"{key} {raw!r} is not a semver version")
    try:
        return parse_semver(raw)
    except ValueError as exc:
        raise SkinVersionError(f"{key} {raw!r} is not a semver version") from exc


def check_skin_version(manifest: Mapping[str, Any], app_version: str, supported_schemas: Iterable[int]) -> SkinVersion:
    """Refuse an unknown schema, bad semver or a skin needing a newer app."""
    supported = sorted(set(supported_schemas))
    schema = manifest.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema not in supported:
        raise SkinVersionError(f"schema {schema!r} is not supported; supported: {supported}")
    version = _semver_key(manifest, "version")
    min_app = _semver_key(manifest, "min_app_version")
    app = parse_semver(app_version)
    if min_app > app:
        raise SkinVersionError(f"min_app_version {manifest['min_app_version']} is newer than app version {app_version}")
    return SkinVersion(schema, version, min_app)
