"""Stem hydration sources: direct R2 credentials or hub-presigned URLs (ADR-0051).

The installed app has no Doppler and must not hold R2 bucket keys. When
CloudSync is configured it hydrates through short-lived presigned GET URLs
minted by the sync hub, which alone holds :class:`apps.cloud.config.CloudConfig`.
A credentialed workstation keeps the direct-R2 path unchanged.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from apps.cloud import asset_store, hydration, stem_index
from apps.cloud.asset_store import AssetS3Client
from apps.cloud.config import CloudConfig
from apps.shared.state.machine_identity import get_or_create_machine_id
from apps.sync_hub import config as sync_config
from apps.sync_hub import spoke_credential
from apps.sync_hub.transport import HttpTransport, HubTransport, SyncTransportError

log = logging.getLogger(__name__)

STEM_HUB_UNREACHABLE = "STEM_HUB_UNREACHABLE"
STEM_HUB_AUTH_REFUSED = "STEM_HUB_AUTH_REFUSED"
STEM_HUB_INDEX_FAILED = "STEM_HUB_INDEX_FAILED"
STEM_BUNDLE_NOT_INDEXED = "STEM_BUNDLE_NOT_INDEXED"
STEM_BUNDLE_PRESIGN_FAILED = "STEM_BUNDLE_PRESIGN_FAILED"
STEM_HYDRATION_NOT_ARMED = "STEM_HYDRATION_NOT_ARMED"

SourceMode = Literal["direct_r2", "hub_presigned"]
UnarmedKind = Literal["transient", "structural"]


class StemSourceError(RuntimeError):
    """A hydration source failure with a stable wire code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class StemHydrationSource(Protocol):
    """How :mod:`apps.cloud.stem_hydration` reaches remote bundle bytes."""

    @property
    def mode(self) -> SourceMode: ...

    def refresh_index(self, data_dir: Path, *, force: bool = False) -> bool: ...

    def refresh_error(self, data_dir: Path) -> str | None: ...

    def bundle_remote_size(
        self, file_hashes: dict[str, str], *, stable_id: str
    ) -> int | None: ...

    def fetch_bundle_files(
        self,
        *,
        stable_id: str,
        file_hashes: dict[str, str],
        tmp_dir: Path,
    ) -> int: ...


@dataclass(frozen=True)
class StemHydrationArmResult:
    """Boot-time stem hydration wiring: armed source or a loud unarmed reason."""

    source: StemHydrationSource | None
    unarmed_reason: str | None
    unarmed_kind: UnarmedKind | None = None


_HTTP_STATUS_RE = re.compile(r"HTTP (\d+)")


def hub_transport_failure_kind(
    exc: StemSourceError,
) -> Literal["unreachable", "hub_5xx"] | None:
    """Classify hub transport failures for bulk partial-result and HTTP mapping."""
    if exc.code == STEM_HUB_UNREACHABLE:
        return "unreachable"
    if exc.code == STEM_HUB_INDEX_FAILED:
        match = _HTTP_STATUS_RE.search(exc.message)
        if match is not None and int(match.group(1)) >= 500:
            return "hub_5xx"
    return None


def classify_stem_hydration_unarmed(code: str | None, message: str) -> UnarmedKind:
    """Classify why hydration stayed unarmed: retry on miss or terminal."""
    if code == STEM_HUB_AUTH_REFUSED:
        return "structural"
    lowered = message.casefold()
    if "boto3 is required" in lowered:
        return "structural"
    if "sync credential" in lowered and "re-enroll" in lowered:
        return "structural"
    if "credential unusable" in lowered or "credential file" in lowered:
        return "structural"
    if code == STEM_HUB_UNREACHABLE:
        return "transient"
    if code == STEM_HUB_INDEX_FAILED:
        return "transient"
    return "transient"


def _unarmed_result(reason: str, *, code: str | None = None) -> StemHydrationArmResult:
    return StemHydrationArmResult(
        None,
        reason,
        classify_stem_hydration_unarmed(code, reason),
    )


@dataclass(frozen=True)
class DirectR2Source:
    """Hydrate through process-local R2 credentials (dev / credentialed host)."""

    cfg: CloudConfig
    s3: AssetS3Client

    @property
    def mode(self) -> SourceMode:
        return "direct_r2"

    def refresh_index(self, data_dir: Path, *, force: bool = False) -> bool:
        return stem_index.refresh_local_cache_throttled(
            data_dir,
            lambda: stem_index.refresh_local_cache_from_r2(self.cfg, self.s3, data_dir),
            force=force,
        )

    def refresh_error(self, data_dir: Path) -> str | None:
        return stem_index.refresh_error(data_dir)

    def bundle_remote_size(
        self,
        file_hashes: dict[str, str],
        *,
        stable_id: str,  # noqa: ARG002 - callers pass stable_id=; size is hash-keyed
    ) -> int | None:
        total = 0
        for digest in file_hashes.values():
            head = asset_store.head_asset(self.cfg, self.s3, digest)
            if head is None:
                return None
            total += head.size
        return total

    def fetch_bundle_files(
        self,
        *,
        stable_id: str,
        file_hashes: dict[str, str],
        tmp_dir: Path,
    ) -> int:
        total = 0
        for filename, digest in file_hashes.items():
            dest = tmp_dir / filename
            head = asset_store.head_asset(self.cfg, self.s3, digest)
            bytes_total = head.size if head is not None else None
            hydration.fetch_asset_for_hydration(
                self.cfg,
                self.s3,
                digest,
                dest,
                stable_id=stable_id,
                bytes_total=bytes_total,
            )
            total += dest.stat().st_size
        return total


@dataclass
class HubPresignedSource:
    """Hydrate through the sync hub's presigned GET URLs (installed app)."""

    data_dir: Path
    hub_url: str
    machine_id: str
    bearer: str | None
    transport: HubTransport | None = None

    @property
    def mode(self) -> SourceMode:
        return "hub_presigned"

    def _transport(self) -> HubTransport:
        if self.transport is not None:
            return self.transport
        return HttpTransport(self.hub_url, bearer=self.bearer)

    def _map_transport_error(self, exc: SyncTransportError, *, label: str) -> StemSourceError:
        if exc.status_code == 401:
            return StemSourceError(
                STEM_HUB_AUTH_REFUSED,
                f"{label}: hub refused sync credential ({exc})",
            )
        if exc.status_code is not None:
            if exc.code == STEM_BUNDLE_NOT_INDEXED:
                return StemSourceError(STEM_BUNDLE_NOT_INDEXED, str(exc))
            if exc.code == STEM_BUNDLE_PRESIGN_FAILED:
                return StemSourceError(STEM_BUNDLE_PRESIGN_FAILED, str(exc))
            return StemSourceError(
                STEM_HUB_INDEX_FAILED,
                f"{label}: hub answered HTTP {exc.status_code} ({exc})",
            )
        return StemSourceError(STEM_HUB_UNREACHABLE, f"{label}: {exc}")

    def refresh_index(self, data_dir: Path, *, force: bool = False) -> bool:
        from apps.cloud.hub_stem_client import fetch_hub_stem_index

        def _fetch() -> stem_index.StemAssetIndex:
            try:
                return fetch_hub_stem_index(
                    self._transport(),
                    machine_id=self.machine_id,
                )
            except SyncTransportError as exc:
                raise self._map_transport_error(exc, label="stem index refresh") from exc

        try:
            return stem_index.refresh_local_cache_throttled(
                data_dir, _fetch, force=force
            )
        except StemSourceError:
            raise
        except Exception as exc:
            raise StemSourceError(STEM_HUB_INDEX_FAILED, str(exc)) from exc

    def refresh_error(self, data_dir: Path) -> str | None:
        return stem_index.refresh_error(data_dir)

    def bundle_remote_size(
        self, _file_hashes: dict[str, str], *, stable_id: str | None = None
    ) -> int | None:
        from apps.cloud.hub_stem_client import fetch_hub_bundle_presign

        if stable_id is None:
            return None
        try:
            presign = fetch_hub_bundle_presign(
                self._transport(),
                machine_id=self.machine_id,
                stable_id=stable_id,
            )
        except SyncTransportError as exc:
            raise self._map_transport_error(exc, label="bundle presign") from exc
        return sum(int(entry["size_bytes"]) for entry in presign["files"])

    def fetch_bundle_files(
        self,
        *,
        stable_id: str,
        file_hashes: dict[str, str],
        tmp_dir: Path,
    ) -> int:
        from apps.cloud.hub_stem_client import fetch_hub_bundle_presign

        try:
            presign = fetch_hub_bundle_presign(
                self._transport(),
                machine_id=self.machine_id,
                stable_id=stable_id,
            )
        except SyncTransportError as exc:
            raise self._map_transport_error(exc, label="bundle presign") from exc
        total = 0
        for entry in presign["files"]:
            filename = entry["filename"]
            digest = entry["content_hash"]
            expected = file_hashes.get(filename)
            if expected is not None and expected != digest:
                raise StemSourceError(
                    STEM_BUNDLE_PRESIGN_FAILED,
                    (
                        f"hub presign digest for {filename} ({digest}) "
                        f"does not match local index ({expected})"
                    ),
                )
            dest = tmp_dir / filename
            hydration.fetch_asset_for_hydration(
                None,
                None,
                digest,
                dest,
                stable_id=stable_id,
                bytes_total=int(entry["size_bytes"]),
                presigned_url=entry["url"],
            )
            total += dest.stat().st_size
        return total


def _cloudsync_configured(data_dir: Path) -> bool:
    effective = sync_config.resolve_config(data_dir)
    return effective.configured and effective.hub_url is not None


def _arm_direct_r2_source() -> StemHydrationArmResult | None:
    """Arm direct-R2 hydration when env credentials resolve.

    Returns an armed or unarmed result when R2 is configured; ``None`` when env
    credentials are absent so the hub path can be tried.
    """
    from apps.cloud.config import CloudConfig, MissingEnvError

    try:
        cfg = CloudConfig.from_env()
        asset_store.require_credentials(cfg)
    except MissingEnvError:
        return None
    try:
        s3 = asset_store.boto3_asset_client(cfg)
    except asset_store.AssetStoreError as exc:
        log.warning(
            "stem-hydration: R2 credentials resolve but hydration is NOT armed: %s",
            exc,
        )
        return _unarmed_result(str(exc))
    return StemHydrationArmResult(DirectR2Source(cfg=cfg, s3=s3), None, None)


def _arm_hub_presigned_source(data_dir: Path) -> StemHydrationArmResult:
    """Arm hub-presigned hydration or record why it stayed unarmed."""
    if not _cloudsync_configured(data_dir):
        return StemHydrationArmResult(None, None, None)
    effective = sync_config.resolve_config(data_dir)
    hub_url = effective.hub_url
    if hub_url is None:
        return StemHydrationArmResult(None, None, None)
    machine_id = get_or_create_machine_id(data_dir)
    try:
        bearer = spoke_credential.read_credential(data_dir)
    except spoke_credential.SpokeCredentialError as exc:
        log.warning("stem-hydration: CloudSync configured but credential unusable: %s", exc)
        return StemHydrationArmResult(None, str(exc), "structural")
    from apps.sync_hub import machine_credentials

    if bearer is None and machine_credentials.configured_mode() == "enforce":
        reason = (
            "CloudSync is enabled but this machine has no stored sync credential; "
            "re-enroll with the hub before stem hydration can arm"
        )
        log.warning("stem-hydration: %s", reason)
        return StemHydrationArmResult(None, reason, "structural")
    source = HubPresignedSource(
        data_dir=data_dir,
        hub_url=hub_url,
        machine_id=machine_id,
        bearer=bearer,
    )
    try:
        source.refresh_index(data_dir, force=True)
    except StemSourceError as exc:
        log.warning(
            "stem-hydration: CloudSync configured but hub index refresh failed: %s",
            exc.message,
        )
        return _unarmed_result(exc.message, code=exc.code)
    # Boot arming must survive unexpected refresh failures without crashing.
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "stem-hydration: CloudSync configured but hub index refresh failed: %s",
            exc,
        )
        return _unarmed_result(str(exc))
    return StemHydrationArmResult(source, None, None)


def arm_stem_hydration_source(data_dir: Path) -> StemHydrationArmResult:
    """Boot-time wiring: arm a source or record why hydration stays unarmed.

    Local mode and machines with neither R2 credentials nor CloudSync configured
    are legitimate unconfigured states (``unarmed_reason`` is ``None``). A
    machine that IS configured for direct R2 or hub hydration but cannot arm
    records ``unarmed_reason`` so stems misses answer 502
    ``STEM_HYDRATION_NOT_ARMED`` instead of the ordinary empty state.
    """
    from apps.cloud import policy

    data_dir = Path(data_dir)
    if policy.CFG.mode != "cloud":
        return StemHydrationArmResult(None, None, None)
    direct = _arm_direct_r2_source()
    if direct is not None:
        return direct
    return _arm_hub_presigned_source(data_dir)


def resolve_stem_hydration_source(data_dir: Path) -> StemHydrationSource | None:
    """Runtime source selection for workers and one-off callers.

    Unlike :func:`arm_stem_hydration_source`, this does not force a boot-style
    index refresh or record ``unarmed_reason``: callers own refresh/hydrate
    failures. It only answers whether this machine has a hydration transport
    configured (direct R2 credentials or an enrolled CloudSync hub).
    """
    from apps.cloud import policy
    from apps.cloud.config import CloudConfig, MissingEnvError

    data_dir = Path(data_dir)
    if policy.CFG.mode != "cloud":
        return None
    try:
        cfg = CloudConfig.from_env()
        asset_store.require_credentials(cfg)
    except MissingEnvError:
        cfg = None
    if cfg is not None:
        try:
            s3 = asset_store.boto3_asset_client(cfg)
        except asset_store.AssetStoreError:
            return None
        return DirectR2Source(cfg=cfg, s3=s3)
    if not _cloudsync_configured(data_dir):
        return None
    effective = sync_config.resolve_config(data_dir)
    hub_url = effective.hub_url
    if hub_url is None:
        return None
    machine_id = get_or_create_machine_id(data_dir)
    try:
        bearer = spoke_credential.read_credential(data_dir)
    except spoke_credential.SpokeCredentialError:
        return None
    from apps.sync_hub import machine_credentials

    if bearer is None and machine_credentials.configured_mode() == "enforce":
        return None
    return HubPresignedSource(
        data_dir=data_dir,
        hub_url=hub_url,
        machine_id=machine_id,
        bearer=bearer,
    )


__all__ = [
    "STEM_BUNDLE_NOT_INDEXED",
    "STEM_BUNDLE_PRESIGN_FAILED",
    "STEM_HUB_AUTH_REFUSED",
    "STEM_HUB_INDEX_FAILED",
    "STEM_HUB_UNREACHABLE",
    "STEM_HYDRATION_NOT_ARMED",
    "DirectR2Source",
    "HubPresignedSource",
    "StemHydrationArmResult",
    "StemHydrationSource",
    "StemSourceError",
    "UnarmedKind",
    "arm_stem_hydration_source",
    "classify_stem_hydration_unarmed",
    "hub_transport_failure_kind",
    "resolve_stem_hydration_source",
]
