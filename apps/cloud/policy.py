"""Resolved CloudSync storage policy.

This module is deliberately data-only. ``CFG`` is loaded once from
``$MDT_DATA_DIR/state/cloudsync-policy.json`` (or ``data/state``) and can be
overridden only by ``MUSIC_DJ_CLOUDSYNC_MODE``. Runtime callers receive the
already-resolved object: they cannot supply a cloud/local mode argument.

The configured mode is the entitlement boundary. Entitlement wiring belongs to
#1451; this object records the result that wiring must select, and never asks a
caller, an agent, or a playback path to make that decision.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

from apps.shared.platform_paths import PROJECT_ROOT

CloudSyncMode = Literal["cloud", "local"]
SourceOfTruth = Literal["r2", "local_machine"]
PolicyMode = Literal["pinned", "cached", "stream", "excluded"]
MachineClass = Literal["workstation", "portable", "server", "archive"]

ARTIFACT_KINDS: tuple[str, ...] = (
    "audio",
    "stem_bundle",
    "anlz_cache",
    "vocal_cache",
    "lyrics_cache",
    "karaoke_words",
)
MACHINE_CLASSES: tuple[MachineClass, ...] = (
    "workstation",
    "portable",
    "server",
    "archive",
)
R2_CONTENT_ADDRESSED_KEY: str = "assets/{sha256[:2]}/{sha256}"
POLICY_FILENAME: str = "cloudsync-policy.json"
MODE_ENV: str = "MUSIC_DJ_CLOUDSYNC_MODE"


class CloudSyncPolicyError(ValueError):
    """Raised when the CloudSync policy configuration is malformed."""


@dataclass(frozen=True)
class ArtifactPolicy:
    """Storage facts for one artifact class under one resolved top-level mode."""

    source_of_truth: SourceOfTruth
    r2_key_scheme: str | None
    local_cache_path: str
    default_mode_by_machine_class: Mapping[MachineClass, PolicyMode]
    cache_budget_mb: int
    eviction_rule: str


@dataclass(frozen=True)
class CloudSyncPolicy:
    """One entitlement-selected, machine-readable CloudSync policy."""

    mode: CloudSyncMode
    remote_processing_allowed: bool
    artifacts: Mapping[str, ArtifactPolicy]
    known_constraints: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation for humans and agents."""
        return {
            "mode": self.mode,
            "remote_processing_allowed": self.remote_processing_allowed,
            "artifacts": {
                kind: {
                    "source_of_truth": artifact.source_of_truth,
                    "r2_key_scheme": artifact.r2_key_scheme,
                    "local_cache_path": artifact.local_cache_path,
                    "default_mode_by_machine_class": dict(artifact.default_mode_by_machine_class),
                    "cache_budget_mb": artifact.cache_budget_mb,
                    "eviction_rule": artifact.eviction_rule,
                }
                for kind, artifact in self.artifacts.items()
            },
            "known_constraints": self.known_constraints,
        }


_ARTIFACT_LAYOUT: dict[str, tuple[str, int]] = {
    "audio": ("state/audio-cache/{sha256[:2]}/{sha256}", 102_400),
    "stem_bundle": ("state/stems/{stable_id}", 102_400),
    "anlz_cache": ("state/anlz-cache/{stable_id}.json", 2_048),
    "vocal_cache": ("state/vocal-cache/{stable_id}.json", 1_024),
    "lyrics_cache": ("state/lyrics-cache/{stable_id}.json", 256),
    "karaoke_words": ("state/karaoke-cache/{stable_id}.json", 512),
}
_CLOUD_DEFAULTS: dict[str, dict[MachineClass, PolicyMode]] = {
    "audio": {
        "workstation": "pinned",
        "portable": "cached",
        "server": "cached",
        "archive": "pinned",
    },
    "stem_bundle": {
        "workstation": "cached",
        "portable": "stream",
        "server": "cached",
        "archive": "pinned",
    },
    "anlz_cache": {
        "workstation": "pinned",
        "portable": "pinned",
        "server": "pinned",
        "archive": "pinned",
    },
    "vocal_cache": {
        "workstation": "pinned",
        "portable": "pinned",
        "server": "pinned",
        "archive": "pinned",
    },
    "lyrics_cache": {
        "workstation": "pinned",
        "portable": "pinned",
        "server": "pinned",
        "archive": "pinned",
    },
    "karaoke_words": {
        "workstation": "pinned",
        "portable": "pinned",
        "server": "pinned",
        "archive": "pinned",
    },
}
_LOCAL_DEFAULTS: dict[MachineClass, PolicyMode] = {
    machine_class: "pinned" for machine_class in MACHINE_CLASSES
}
_KNOWN_CONSTRAINTS: tuple[str, ...] = (
    "Cloud mode requires an enabled R2 service and R2_ACCOUNT_ID / "
    "R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY in the process environment.",
    "Legacy object layouts require a separate migration; this policy names "
    "the target content-addressed key and performs no migration.",
    "Content-addressed resolution requires populated "
    "track_locations.content_hash values.",
    "This policy does not change stem routing; destination migration is "
    "a separate operation.",
)


def policy_config_path() -> Path:
    """Return the sole on-disk policy configuration path."""
    configured_data_dir = os.environ.get("MDT_DATA_DIR")
    root = Path(configured_data_dir) if configured_data_dir else PROJECT_ROOT / "data"
    return Path(root) / "state" / POLICY_FILENAME


def _read_config(path: Path) -> dict[str, object]:
    if path.is_symlink() and not path.exists():
        raise CloudSyncPolicyError(f"CloudSync policy symlink target is missing: {path}")
    if not path.exists():
        return {}
    if not path.is_file():
        raise CloudSyncPolicyError(f"CloudSync policy path is not a file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CloudSyncPolicyError(f"invalid JSON in CloudSync policy {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CloudSyncPolicyError(f"CloudSync policy {path} must be a JSON object")
    unknown = set(value).difference({"mode"})
    if unknown:
        raise CloudSyncPolicyError(
            f"CloudSync policy {path} has unsupported keys: {sorted(unknown)}"
        )
    return value


def _configured_mode(config: Mapping[str, object]) -> CloudSyncMode:
    candidate = os.environ.get(MODE_ENV, config.get("mode", "cloud"))
    if not isinstance(candidate, str) or candidate not in {"cloud", "local"}:
        raise CloudSyncPolicyError(
            f"{MODE_ENV} / policy mode must be 'cloud' or 'local'; got {candidate!r}"
        )
    return cast(CloudSyncMode, candidate)


def _artifacts_for(mode: CloudSyncMode) -> dict[str, ArtifactPolicy]:
    source: SourceOfTruth = "r2" if mode == "cloud" else "local_machine"
    r2_key_scheme = R2_CONTENT_ADDRESSED_KEY if mode == "cloud" else None
    artifacts: dict[str, ArtifactPolicy] = {}
    for kind in ARTIFACT_KINDS:
        local_cache_path, cache_budget_mb = _ARTIFACT_LAYOUT[kind]
        defaults: dict[MachineClass, PolicyMode] = (
            _CLOUD_DEFAULTS[kind] if mode == "cloud" else _LOCAL_DEFAULTS
        )
        artifacts[kind] = ArtifactPolicy(
            source_of_truth=source,
            r2_key_scheme=r2_key_scheme,
            local_cache_path=local_cache_path,
            default_mode_by_machine_class=MappingProxyType(dict(defaults)),
            cache_budget_mb=cache_budget_mb,
            eviction_rule="lru_atime_for_cached",
        )
    return artifacts


def load_policy() -> CloudSyncPolicy:
    """Load the entitlement-selected policy from config and its env override.

    The absence of a local config uses the documented paid ``cloud`` policy.
    Only the deployment configuration or its explicit environment override can
    select a mode. There is intentionally no ``mode=`` argument for callers.
    """
    config = _read_config(policy_config_path())
    mode = _configured_mode(config)
    return CloudSyncPolicy(
        mode=mode,
        remote_processing_allowed=mode == "cloud",
        artifacts=MappingProxyType(_artifacts_for(mode)),
        known_constraints=_KNOWN_CONSTRAINTS,
    )


CFG: CloudSyncPolicy = load_policy()


def artifact_cache_root(data_dir: Path, asset_kind: str) -> Path:
    """Return the directory under which content-addressed cache files live."""
    if asset_kind not in CFG.artifacts:
        raise CloudSyncPolicyError(
            f"unknown asset_kind {asset_kind!r}; known: {sorted(CFG.artifacts)}"
        )
    template = CFG.artifacts[asset_kind].local_cache_path
    placeholder = template.find("/{")
    if placeholder == -1:
        return data_dir / Path(template).parent
    return data_dir / template[:placeholder]


def cache_budget_mb_for(asset_kind: str) -> int:
    """Return ``CFG.artifacts[kind].cache_budget_mb``."""
    if asset_kind not in CFG.artifacts:
        raise CloudSyncPolicyError(
            f"unknown asset_kind {asset_kind!r}; known: {sorted(CFG.artifacts)}"
        )
    return CFG.artifacts[asset_kind].cache_budget_mb


def main() -> None:
    """Print the resolved policy for ``just cloudsync-policy``."""
    print(json.dumps(CFG.as_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = [
    "ARTIFACT_KINDS",
    "CFG",
    "MACHINE_CLASSES",
    "MODE_ENV",
    "POLICY_FILENAME",
    "R2_CONTENT_ADDRESSED_KEY",
    "ArtifactPolicy",
    "CloudSyncMode",
    "CloudSyncPolicy",
    "CloudSyncPolicyError",
    "artifact_cache_root",
    "cache_budget_mb_for",
    "load_policy",
    "policy_config_path",
]
