"""Part of `plugins_api.py`, split out with no behavior change: store."""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ParamSpec, TypeVar

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.plugins.catalog import (
    CatalogCache,
    CatalogEntry,
    CatalogError,
    CatalogRead,
    Fetcher,
    http_fetch,
    read_channel,
)
from apps.plugins.host import PluginRuntime
from apps.plugins.installer import (
    InstallError,
    receipt_archive_sha256,
)
from apps.plugins.listing import Listing
from apps.plugins.manifest import CHANNELS, PluginManifest, semver_key
from apps.plugins.providers import ProviderSurface
from apps.plugins.state import (
    STATE_LOCK,
    InstalledPlugin,
    PluginState,
    StateFileError,
    load_state,
    save_state,
)
from apps.shared.app_version import APP_VERSION

P = ParamSpec("P")
R = TypeVar("R")

API_PREFIX: str = "/api/v1"
RUNTIME_STATE_ATTR: str = "plugins"

router = APIRouter(prefix="/plugins", tags=["plugins"])


STORE_CONFIG_ATTR: str = "plugin_store_config"


@dataclass(frozen=True)
class StoreConfig:
    """Where catalogs and archives come from, and which keys sign them.

    Production uses the defaults: the https fetcher, the compiled-in base URL
    and :data:`apps.plugins.signing.TRUSTED_STORE_KEYS`. Tests build the app
    with a local fetcher and a throwaway key; nothing outside the process can
    reach this object, so it cannot widen trust at runtime.
    """

    fetch: Fetcher = http_fetch
    base_url: str | None = None
    trusted: Mapping[str, str] | None = None
    #: The running build, checked against a plugin's ``min_app_version``.
    app_version: str = APP_VERSION


def _require_loaded(plugin_id: str) -> Callable[[Request], None]:
    """Close a plugin's routes unless it is still ``loaded`` (a startup hook may fail after mount)."""

    def check(request: Request) -> None:
        if not _runtime(request).loaded(plugin_id):
            raise _refuse(503, "not-loaded", f"{plugin_id} did not finish starting this session")

    return check


def _runtime(request: Request) -> PluginRuntime:
    runtime = getattr(request.app.state, RUNTIME_STATE_ATTR, None)
    if runtime is None:
        raise HTTPException(503, {"code": "not-mounted", "message": "plugin runtime not mounted"})
    return runtime


def _config(request: Request) -> StoreConfig:
    return getattr(request.app.state, STORE_CONFIG_ATTR, None) or StoreConfig()


def _read(request: Request, channel: str, *, refresh: bool) -> CatalogRead:
    cfg = _config(request)
    read = read_channel(
        channel,
        CatalogCache(_root(request)),
        fetch=cfg.fetch,
        base_url=cfg.base_url,
        refresh=refresh,
        trusted=cfg.trusted,
    )
    _note_verified(_root(request), [read])
    return read


def _note_verified(root: Path, reads: Iterable[CatalogRead]) -> None:
    """Record each channel whose catalog verified, so boot requires its blocklist from now on.

    A channel with a replay-floor record verified here too, even when its
    cache write then failed and the read carries no catalog: that index may
    revoke an installed plugin, so boot must wait for it.
    """
    cache = CatalogCache(root)
    verified = frozenset(read.channel for read in reads if read.catalog is not None or _seen(cache, read.channel))
    if verified <= load_state(root).blocklist_channels:
        return
    with STATE_LOCK:
        state = load_state(root)
        save_state(root, replace(state, blocklist_channels=state.blocklist_channels | verified))


def _seen(cache: CatalogCache, channel: str) -> bool:
    """A catalog for ``channel`` has verified here: its replay-floor record exists (a damaged one counts)."""
    try:
        return cache.newest(channel) is not None
    except CatalogError:
        return True


def _root(request: Request) -> Path:
    return _runtime(request).plugins_dir


def _serialized(route: Callable[P, R]) -> Callable[P, R]:
    """Run a route that rewrites the state file under :data:`STATE_LOCK`.

    Each one loads the state, may fetch an archive, then saves; without the
    lock two clicks at once lose one of the changes.
    """

    @functools.wraps(route)
    def locked(*args: P.args, **kwargs: P.kwargs) -> R:
        with STATE_LOCK:
            return route(*args, **kwargs)

    return locked


def _refuse(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def _save(root: Path, state: PluginState) -> None:
    """A route's state write: a refused write is the named 409, never an unhandled 500."""
    try:
        save_state(root, state)
    except StateFileError as exc:
        raise _refuse(409, "state-full", str(exc)) from exc


# ----- models ---------------------------------------------------------------
class FaultOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str
    message: str


class ChannelOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    channel: str
    url: str
    key_id: str | None
    generated_at: str | None
    fetched_at: str | None
    stale: bool
    fault: FaultOut | None
    visible: bool


class ScreenshotOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    #: Engine-relative URL of the verified image.
    url: str
    width: int
    height: int
    alt: str
    #: "dark", "light" or "any": which skin the screenshot shows.
    theme: str


class ListingOut(BaseModel):
    """Problem first (PLUG-6522k): why you want it, what you can do, how, and the limits."""

    model_config = ConfigDict(frozen=True)
    tagline: str
    problem: str
    you_can: list[str]
    how_it_works: str
    limits: list[str]
    status: str
    screenshots: list[ScreenshotOut]
    capability_kind: str | None = None
    store_surface: str | None = None


class ProviderQuotaOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    limit: int | None
    remaining: int | None
    reset_at: int | None
    retry_after_s: int | None


class ProviderSurfaceOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    capability_kind: str
    store_surface: str
    credential_env: str | None
    credential_configured: bool
    terms_url: str | None
    provenance_fields: list[str]
    quota: ProviderQuotaOut | None = None


class PluginRowOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    plugin_id: str
    name: str
    summary: str
    publisher: str
    trust: str
    kind: str
    tier: int
    channel: str
    license: str
    permissions: list[str]
    notice: str | None
    homepage: str | None
    available_version: str | None
    #: The signed archive digest of ``available_version``; an install sends it back.
    available_sha256: str | None
    installed_version: str | None
    source: str | None
    enabled: bool
    #: This boot's outcome: loaded, blocked, dev-only, tampered, conflict,
    #: missing-dependency, failed; None when it was not enabled at boot.
    load_status: str | None
    load_detail: str | None
    #: "enable", "disable", "update" or None: what the next start will change.
    pending: str | None
    update_available: bool
    #: The signed store listing; None for an installed plugin no catalog lists.
    listing: ListingOut | None
    provider: ProviderSurfaceOut | None


class PluginsOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    dev_mode: bool
    labs_opt_in: bool
    restart_required: bool
    channels: list[ChannelOut]
    plugins: list[PluginRowOut]


class InstallIn(BaseModel):
    channel: str
    accept_permissions: list[str]
    acknowledge_notice: bool = False
    #: The exact terms notice that was on screen, so an acknowledgement cannot
    #: carry over to a notice a catalog refresh changed since.
    notice: str | None = None
    #: The build that was on screen (the row's ``available_sha256``): consent is to
    #: that archive, so a catalog refresh that swapped it since is refused.
    archive_sha256: str


class LabsIn(BaseModel):
    opt_in: bool


class SideloadIn(BaseModel):
    path: str
    #: Same consent a store install needs: exactly the build's permissions ...
    accept_permissions: list[str] = Field(default_factory=list)
    #: ... and, for a module carrying a terms notice, its acknowledgement ...
    acknowledge_notice: bool = False
    #: ... all for exactly the build the preview showed: its manifest's sha256, so
    #: consent cannot carry over to a rebuilt or replaced source.
    manifest_sha256: str


class SideloadPreviewIn(BaseModel):
    path: str


class SideloadPreviewOut(BaseModel):
    plugin_id: str
    name: str
    version: str
    channel: str
    permissions: list[str]
    notice: str | None
    #: Pass back on sideload; a source whose manifest changed since is refused.
    manifest_sha256: str


class ChangeOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    plugin_id: str
    version: str | None
    enabled: bool
    restart_required: bool
    message: str


class UiSlotOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    plugin_id: str
    version: str
    slot: str
    url: str


# ----- helpers --------------------------------------------------------------
def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")


def _rebuilt(available: CatalogEntry, installed: InstalledPlugin, runtime: PluginRuntime) -> bool:
    """The store build at the installed store build's own version repairs it.

    It does when the store now ships different bytes, or when this boot
    refused the very install on disk as tampered (a lost or broken receipt
    signature leaves the unverified digest matching). A sideload is never
    offered the store's build this way.
    """
    if not installed.source.startswith("store:") or available.manifest.version != installed.version:
        return False
    outcome = runtime.outcomes.get(installed.plugin_id)
    refused = (
        outcome is not None
        and outcome.status == "tampered"
        and outcome.version == installed.version
        and runtime.install_ids.get(installed.plugin_id) == installed.install_id
    )
    return refused or available.archive_sha256 != receipt_archive_sha256(
        runtime.plugins_dir, installed.plugin_id, installed.version
    )


def _provider_out(surface: ProviderSurface | None) -> ProviderSurfaceOut | None:
    if surface is None:
        return None
    quota = surface.quota
    return ProviderSurfaceOut(
        capability_kind=surface.capability_kind,
        store_surface=surface.store_surface,
        credential_env=surface.credential_env,
        credential_configured=surface.credential_configured,
        terms_url=surface.terms_url,
        provenance_fields=list(surface.provenance_fields),
        quota=ProviderQuotaOut(**quota.to_wire()) if quota is not None else None,
    )


def _describes(available: CatalogEntry, manifest: PluginManifest) -> bool:
    """Whether the store's listing describes the build a row shows, not an older or other one."""
    return available.manifest.to_wire() == manifest.to_wire()


def _offers_update(available: CatalogEntry, installed: InstalledPlugin, runtime: PluginRuntime) -> bool:
    return semver_key(available.manifest.version) > semver_key(installed.version) or _rebuilt(
        available, installed, runtime
    )


def _shown_manifest(entry: CatalogEntry, installed: InstalledPlugin | None, runtime: PluginRuntime) -> PluginManifest:
    """The build a row describes: the store's when it is on offer, else the installed one.

    A sideload newer than the catalog's build would otherwise be labeled with
    the older build's name, trust, notice and permissions.
    """
    if installed is None or _offers_update(entry, installed, runtime):
        return entry.manifest
    return _installed_manifest(runtime.plugins_dir, installed) or entry.manifest


def _listing_out(plugin_id: str, listing: Listing) -> ListingOut:
    return ListingOut(
        tagline=listing.tagline,
        problem=listing.problem,
        you_can=list(listing.you_can),
        how_it_works=listing.how_it_works,
        limits=list(listing.limits),
        status=listing.status,
        screenshots=[
            ScreenshotOut(
                url=f"{API_PREFIX}/plugins/{plugin_id}/media/{shot.sha256}",
                width=shot.width,
                height=shot.height,
                alt=shot.alt,
                theme=shot.theme,
            )
            for shot in listing.screenshots
        ],
        capability_kind=listing.capability_kind,
        store_surface=listing.store_surface,
    )


def _installed_manifest(root: Path, plugin: InstalledPlugin) -> PluginManifest | None:
    from apps.plugins.installer import read_installed_manifest

    try:
        return read_installed_manifest(root, plugin.plugin_id, plugin.version)
    except InstallError:
        return None


def _channel_out(read: CatalogRead, labs_opt_in: bool) -> ChannelOut:
    return ChannelOut(
        channel=read.channel,
        url=read.url,
        key_id=read.catalog.key_id if read.catalog else None,
        generated_at=read.catalog.generated_at if read.catalog else None,
        fetched_at=_iso(read.fetched_at),
        stale=read.stale,
        fault=FaultOut(code=read.fault.code, message=str(read.fault)) if read.fault else None,
        visible=read.channel == "stable" or labs_opt_in,
    )


def _clearable_reads(request: Request, channel: str, read: CatalogRead) -> dict[str, CatalogRead]:
    """Every channel's read, refusing unless each channel boot must clear has a verified catalog.

    A channel that is the install's own or was recorded as blocklisting before, with no verified catalog,
    would hold the install off at the next boot, so it is refused here instead (Codex on PR #6522).
    """
    reads = {c: read if c == channel else _read(request, c, refresh=False) for c in CHANNELS}
    for needed in sorted(load_state(_root(request)).blocklist_channels | {channel}):
        unverified = reads[needed]
        if unverified.catalog is None:
            fault = unverified.fault or CatalogError("not-fetched", "no catalog")
            raise _refuse(
                409,
                fault.code,
                f"the {needed} catalog, whose blocklist this install must clear, is unavailable: {fault}",
            )
    return reads


def _consented_entry(request: Request, plugin_id: str, body: InstallIn) -> tuple[CatalogEntry, CatalogRead]:
    """The signed catalog entry and the read it came from, once every consent rule holds."""
    read = _read(request, body.channel, refresh=False)
    if read.catalog is None:
        fault = read.fault or CatalogError("not-fetched", "no catalog")
        raise _refuse(409, fault.code, str(fault))
    entry = read.catalog.find(plugin_id)
    if entry is None:
        raise _refuse(404, "not-in-catalog", f"{plugin_id} is not in the {body.channel} catalog")
    manifest = entry.manifest
    # Every verified channel's blocklist, as boot applies them: a build either one revokes would never load.
    reads = _clearable_reads(request, body.channel, read)
    blocklist = [b for r in reads.values() if r.catalog is not None for b in r.catalog.blocklist]
    if any(b.blocks(plugin_id, manifest.version) for b in blocklist):
        raise _refuse(409, "blocked", f"{plugin_id} {manifest.version} is on the store blocklist")
    _check_consent(entry, body)
    return entry, read


def _check_consent(entry: CatalogEntry, body: InstallIn) -> None:
    """Refuse unless the person consented to exactly this build, its permissions and its notice."""
    manifest = entry.manifest
    if body.archive_sha256 != entry.archive_sha256:
        raise _refuse(
            409,
            "build-changed",
            f"the store now offers a different build of {manifest.plugin_id} ({manifest.version}); review it again",
        )
    if sorted(body.accept_permissions) != sorted(manifest.permissions):
        raise _refuse(
            422,
            "permissions-not-accepted",
            f"accept exactly these permissions to install: {list(manifest.permissions)}",
        )
    if manifest.needs_terms_notice and not (body.acknowledge_notice and body.notice == manifest.notice):
        raise _refuse(422, "notice-not-acknowledged", manifest.notice or "")


def _refuse_replacing_running(runtime: PluginRuntime, plugin_id: str, version: str) -> None:
    """The running version's folder is in use until restart; never rewrite it in place."""
    running = runtime.hosts.get(plugin_id)  # resident code; a failed load left none
    if running is not None and running.manifest.version == version:
        raise InstallError(
            "version-running",
            f"{plugin_id} {version} is running this session; restart the engine first, "
            "or give the new build a new version",
        )


#: HTTP status for each sideload refusal; anything else is a bad source (422).
_SIDELOAD_STATUS = {
    "dev-mode-off": 403,
    "labs-not-enabled": 409,
    "needs-newer-app": 409,
    "version-running": 409,
}
