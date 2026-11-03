"""The plugin store over HTTP, and the boot step that loads enabled modules.

    GET    /api/v1/plugins                    catalog x installed x this boot
    POST   /api/v1/plugins/labs               opt in or out of the labs channel
    POST   /api/v1/plugins/{id}/install       download, verify, install, enable
    POST   /api/v1/plugins/{id}/enable        enable at the next start
    POST   /api/v1/plugins/{id}/disable       disable at the next start
    DELETE /api/v1/plugins/{id}               uninstall
    POST   /api/v1/plugins/sideload           developer install from a folder
    GET    /api/v1/plugins/ui-slots           UI modules the SPA should mount
    GET    /api/v1/plugins/{id}/ui/{path}     a loaded module's UI file
    GET    /api/v1/plugins/{id}/media/{sha}   a listing screenshot, verified

THE SAME SURFACE FOR THE STORE PAGE AND FOR AGENTS. The page holds no logic
of its own: every rule (labs opt-in, third-party terms notice, permission consent,
signed catalog, restart to apply) is enforced here, so an agent driving the
API meets exactly the refusals a user clicking the page sees, worded once.

CHANGES APPLY AT THE NEXT START (PLUG-6522e). Code is loaded only by
:func:`mount_plugins` during ``create_app``. Install, enable, disable and
uninstall change the state file and report ``restart_required``; nothing
here restarts the engine, so no store action can interrupt a playing deck.

Requirements (mini-PRD):
  * ✔︎ ✅ 🎯 every catalog fault is on the wire with its code and URL.
    [if] an unverifiable catalog reads as an empty store [then ⛔️]
  * ✔︎ ✅ 🎯 install refuses without consent to exactly the manifest's
    permissions, a labs install refuses without labs opt-in, a tier-2
    install refuses without the notice acknowledged.
  * ✔︎ ✅ 🎯 a UI file is served only for a plugin loaded this boot, only if
    listed in its manifest, and only while its bytes still match.
  * ✔︎ ✅ 🎯 a screenshot is served only if a visible signed listing pins it,
    and only as bytes that match the pin (PLUG-6522k).
"""

from __future__ import annotations

import functools
import hashlib
import logging
import mimetypes
import os
import tempfile
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, ParamSpec, TypeVar, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from apps.plugins.catalog import (
    CatalogCache,
    CatalogEntry,
    CatalogError,
    CatalogRead,
    Fetcher,
    http_fetch,
    read_channel,
    read_screenshot,
)
from apps.plugins.host import PluginRuntime, load_enabled, taken_routes
from apps.plugins.installer import (
    MAX_ARCHIVE_BYTES,
    MAX_UNPACKED_BYTES,
    InstallError,
    check_app_version,
    dev_mode,
    install_archive,
    install_dir,
    install_directory,
    open_regular,
    preview_directory,
    receipt_archive_sha256,
    receipt_written,
    recover_replacements,
    remove_installed,
    sweep_unrecorded,
)
from apps.plugins.listing import Listing
from apps.plugins.manifest import CHANNELS, UI_DIR, PluginManifest, semver_key
from apps.plugins.state import (
    STATE_LOCK,
    InstalledPlugin,
    PluginState,
    StateFileError,
    load_state,
    plugins_dir,
    save_state,
)
from apps.shared.app_version import APP_VERSION
from apps.shared.semver import Semver

log = logging.getLogger(__name__)
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


# ----- boot -----------------------------------------------------------------
def mount_plugins(
    app: FastAPI, data_dir: Path, *, config: StoreConfig | None = None
) -> PluginRuntime:
    """Load enabled modules and mount their routers. Call before the SPA mount.

    The blocklist comes from the last VERIFIED cached catalogs, so boot never
    touches the network and a revoked version stays off while offline.
    """
    root = plugins_dir(data_dir)
    state = load_state(root)
    held = _recover_on_boot(root, state)
    cache = CatalogCache(root)
    cfg = config or StoreConfig()
    setattr(app.state, STORE_CONFIG_ATTR, cfg)
    # With nothing installed there is no blocklist to apply, so boot reads only
    # the state file (PLUG-6522), not the cached catalogs; the store page reads them.
    reads = [
        read_channel(channel, cache, refresh=False, trusted=cfg.trusted) for channel in CHANNELS
    ] if state.plugins else []
    catalogs = [read.catalog for read in reads if read.catalog is not None]
    if reads:
        _note_verified(root, reads)
        state = load_state(root)
    # The store's own routes are reserved before any plugin claims a path, or a
    # module declaring /plugins would be mounted ahead of them and shadow them.
    taken = taken_routes(app.routes) + taken_routes(router.routes, API_PREFIX)
    runtime = load_enabled(
        root, data_dir, state, catalogs=catalogs, taken=taken,
        trusted=cfg.trusted,
        app_version=cfg.app_version,
        held=held,
    )
    for plugin_id, host in runtime.hosts.items():
        for plugin_router in host.routers:
            app.include_router(plugin_router, prefix=API_PREFIX, dependencies=[Depends(_require_loaded(plugin_id))])
    for outcome in runtime.outcomes.values():
        level = logging.INFO if outcome.status == "loaded" else logging.WARNING
        log.log(level, "plugin %s %s: %s %s", outcome.plugin_id, outcome.version,
                outcome.status, outcome.detail)
    setattr(app.state, RUNTIME_STATE_ATTR, runtime)
    app.state.plugins_data_dir = data_dir
    app.include_router(router, prefix=API_PREFIX)
    return runtime


def _recover_on_boot(root: Path, state: PluginState) -> dict[str, str]:
    """Finish or undo installs a crash interrupted, then drop versions kept only while running.

    Returns the plugins that must stay off this session: an install that could
    not be finished or undone may have new files and receipt under an old record.
    """
    try:
        held = recover_replacements(root, {p.plugin_id: (p.version, p.install_id) for p in state.plugins.values()})
    except (InstallError, OSError) as exc:  # the staging folder itself: no install could be checked
        held = {plugin_id: f"interrupted installs could not be checked: {exc}" for plugin_id in state.plugins}
    try:
        sweep_unrecorded(root, {p.plugin_id: p.version for p in state.plugins.values()})
    except (InstallError, OSError) as exc:
        log.warning("plugin cleanup skipped: %s", exc)  # leftovers cost disk, never correctness
    return held


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


def _pending(installed: InstalledPlugin | None, runtime: PluginRuntime, plugin_id: str) -> str | None:
    outcome = runtime.outcomes.get(plugin_id)
    # Resident code (loaded, or a startup hook failed after register) runs until restart.
    resident = plugin_id in runtime.hosts
    if installed is None:
        return "disable" if resident else None
    if installed.enabled and outcome is None:
        return "enable"
    if not installed.enabled and resident:
        return "disable"
    if installed.enabled and outcome is not None and (
        outcome.version != installed.version or runtime.install_ids.get(plugin_id) != installed.install_id
    ):
        return "update"  # a different version, or the same version reinstalled since this boot
    return None


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


def _row(
    manifest: PluginManifest,
    available: CatalogEntry | None,
    installed: InstalledPlugin | None,
    runtime: PluginRuntime,
) -> PluginRowOut:
    outcome = runtime.outcomes.get(manifest.plugin_id)
    update = available is not None and installed is not None and _offers_update(available, installed, runtime)
    return PluginRowOut(
        plugin_id=manifest.plugin_id,
        name=manifest.name,
        summary=manifest.summary,
        publisher=manifest.publisher,
        trust=manifest.trust,
        kind=manifest.kind,
        tier=manifest.tier,
        channel=manifest.channel,
        license=manifest.license,
        permissions=list(manifest.permissions),
        notice=manifest.notice,
        homepage=manifest.homepage,
        available_version=available.manifest.version if available else None,
        available_sha256=available.archive_sha256 if available else None,
        installed_version=installed.version if installed else None,
        source=installed.source if installed else None,
        enabled=bool(installed and installed.enabled),
        load_status=outcome.status if outcome else None,
        load_detail=outcome.detail if outcome else None,
        pending=_pending(installed, runtime, manifest.plugin_id),
        update_available=update,
        listing=_listing_out(manifest.plugin_id, available.listing)
        if available is not None and _describes(available, manifest)
        else None,
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
                width=shot.width, height=shot.height, alt=shot.alt, theme=shot.theme,
            )
            for shot in listing.screenshots
        ],
    )


def _is_labs(root: Path, plugin: InstalledPlugin) -> bool:
    """A labs module, whether it came from the labs channel or was sideloaded."""
    if plugin.source == "store:labs":
        return True
    manifest = _installed_manifest(root, plugin) if plugin.source == "sideload" else None
    return manifest is not None and manifest.channel == "labs"


def _installed_manifest(root: Path, plugin: InstalledPlugin) -> PluginManifest | None:
    from apps.plugins.installer import read_installed_manifest

    try:
        return read_installed_manifest(root, plugin.plugin_id, plugin.version)
    except InstallError:
        return None


def _change(plugin_id: str, plugin: InstalledPlugin | None, runtime: PluginRuntime, message: str) -> ChangeOut:
    pending = _pending(plugin, runtime, plugin_id)
    return ChangeOut(
        plugin_id=plugin_id,
        version=plugin.version if plugin else None,
        enabled=bool(plugin and plugin.enabled),
        restart_required=pending is not None,
        message=message,
    )


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


def _rows(
    reads: Iterable[CatalogRead], state: PluginState, runtime: PluginRuntime
) -> dict[str, PluginRowOut]:
    """Catalog rows for every visible channel, then installed plugins no catalog lists."""
    rows: dict[str, PluginRowOut] = {}
    for plugin_id, entry in _newest_entries(reads, state.labs_opt_in).items():
        installed = state.plugins.get(plugin_id)
        if not state.labs_opt_in and installed is not None and _is_labs(runtime.plugins_dir, installed):
            continue  # a hidden labs install keeps its own row; a stable listing must not relabel it
        rows[plugin_id] = _row(_shown_manifest(entry, installed, runtime), entry, installed, runtime)
    for plugin_id, installed in state.plugins.items():
        if plugin_id in rows:
            continue
        manifest = _installed_manifest(runtime.plugins_dir, installed)
        if manifest is not None:
            rows[plugin_id] = _row(manifest, None, installed, runtime)
        else:  # its files are missing or broken: still listed, so it can be disabled or uninstalled
            rows[plugin_id] = _unreadable_row(installed, runtime)
    return rows


def _newest_entries(reads: Iterable[CatalogRead], labs_opt_in: bool) -> dict[str, CatalogEntry]:
    """Each visible plugin's entry: its newer build across channels, and stable on a tie,
    whichever order the channels were read in."""
    chosen: dict[str, tuple[tuple[Semver, bool], CatalogEntry]] = {}
    for read in reads:
        if read.catalog is None or (read.channel == "labs" and not labs_opt_in):
            continue
        for entry in read.catalog.entries:
            rank = (semver_key(entry.manifest.version), read.channel == "stable")
            held = chosen.get(entry.manifest.plugin_id)
            if held is None or rank > held[0]:
                chosen[entry.manifest.plugin_id] = (rank, entry)
    return {plugin_id: entry for plugin_id, (_, entry) in chosen.items()}


def _unreadable_row(installed: InstalledPlugin, runtime: PluginRuntime) -> PluginRowOut:
    """A row from the install record alone, for a plugin whose manifest cannot be read."""
    plugin_id = installed.plugin_id
    outcome = runtime.outcomes.get(plugin_id)
    return PluginRowOut(
        plugin_id=plugin_id,
        name=plugin_id,
        summary="Its installed files are missing or damaged. Uninstall it, then install it again.",
        publisher="unknown",
        trust="unknown",
        kind="unknown",
        tier=0,
        channel="stable",
        license="unknown",
        permissions=list(installed.granted_permissions),
        notice=None,
        homepage=None,
        available_version=None,
        available_sha256=None,
        installed_version=installed.version,
        source=installed.source,
        enabled=installed.enabled,
        load_status=outcome.status if outcome else None,
        load_detail=outcome.detail if outcome else None,
        pending=_pending(installed, runtime, plugin_id),
        update_available=False,
        listing=None,
    )


# ----- routes ---------------------------------------------------------------
@router.get("", response_model=PluginsOut)
def list_plugins(request: Request, refresh: bool = False) -> PluginsOut:
    """The store page in one read. ``refresh=true`` re-fetches both catalogs."""
    runtime = _runtime(request)
    state = load_state(runtime.plugins_dir)
    reads = {channel: _read(request, channel, refresh=refresh) for channel in CHANNELS}
    channels = [_channel_out(read, state.labs_opt_in) for read in reads.values()]
    rows = _rows(reads.values(), state, runtime)
    restart = any(row.pending is not None for row in rows.values()) or any(
        plugin_id not in rows for plugin_id in runtime.hosts
    )
    return PluginsOut(
        dev_mode=dev_mode(),
        labs_opt_in=state.labs_opt_in,
        restart_required=restart,
        channels=channels,
        plugins=sorted(rows.values(), key=lambda r: (r.channel != "stable", r.name.lower())),
    )


@router.post("/labs", response_model=PluginsOut)
@_serialized
def set_labs(request: Request, body: LabsIn) -> PluginsOut:
    root = _root(request)
    state = load_state(root)
    if not body.opt_in:
        for plugin in state.plugins.values():
            if plugin.enabled and _is_labs(root, plugin):
                raise _refuse(
                    409, "labs-module-enabled",
                    f"disable {plugin.plugin_id} before leaving the labs channel",
                )
    _save(root, replace(state, labs_opt_in=body.opt_in))
    return list_plugins(request)


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
            raise _refuse(409, fault.code, f"the {needed} catalog, whose blocklist this install must clear, is unavailable: {fault}")
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
            409, "build-changed",
            f"the store now offers a different build of {manifest.plugin_id} ({manifest.version}); review it again",
        )
    if sorted(body.accept_permissions) != sorted(manifest.permissions):
        raise _refuse(
            422, "permissions-not-accepted",
            f"accept exactly these permissions to install: {list(manifest.permissions)}",
        )
    if manifest.needs_terms_notice and not (body.acknowledge_notice and body.notice == manifest.notice):
        raise _refuse(422, "notice-not-acknowledged", manifest.notice or "")


@router.post("/{plugin_id}/install", response_model=ChangeOut)
@_serialized
def install(request: Request, plugin_id: str, body: InstallIn) -> ChangeOut:
    runtime = _runtime(request)
    root = runtime.plugins_dir
    state = load_state(root)
    if body.channel not in CHANNELS:
        raise _refuse(422, "bad-channel", f"channel must be one of {list(CHANNELS)}")
    if body.channel == "labs" and not state.labs_opt_in:
        raise _refuse(409, "labs-not-enabled", "turn on the labs channel before installing from it")
    entry, read = _consented_entry(request, plugin_id, body)
    manifest = entry.manifest
    try:
        check_app_version(manifest, _config(request).app_version)
        _refuse_replacing_running(runtime, plugin_id, manifest.version)
    except InstallError as exc:
        raise _refuse(409, exc.code, str(exc)) from exc
    previous = state.plugins.get(plugin_id)
    plugin = InstalledPlugin(
        plugin_id=plugin_id,
        version=manifest.version,
        enabled=previous.enabled if previous is not None else True,  # an update never turns it back on
        source=f"store:{body.channel}",
        installed_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        granted_permissions=tuple(manifest.permissions),
        notice_acknowledged=manifest.needs_terms_notice,
        accepted_notice_sha256=manifest.notice_sha256,
        install_id=uuid.uuid4().hex,
    )

    def commit() -> None:
        # Receipt and record land before the old copy goes; either failing puts both back.
        with receipt_written(root, manifest, read.payload, read.signature):
            save_state(root, state.with_plugin(plugin))

    try:
        archive = _config(request).fetch(entry.archive_url, max_bytes=MAX_ARCHIVE_BYTES)
        install_archive(root, archive, entry, then=commit, install_id=plugin.install_id)
    except CatalogError as exc:
        raise _refuse(502, exc.code, str(exc)) from exc
    except InstallError as exc:
        raise _refuse(422, exc.code, str(exc)) from exc
    except (OSError, StateFileError) as exc:  # a refused state write is put back like any failed write
        raise _refuse(409, "install-failed", f"could not install {plugin_id}: {exc}") from exc
    keep = [manifest.version]
    loaded = runtime.outcomes.get(plugin_id)
    if loaded is not None:
        keep.append(loaded.version)  # still running this boot; removed next time
    if previous is not None:
        try:  # the install has committed; a locked old copy is left for the boot sweep, not reported as a failure
            remove_installed(root, plugin_id, keep=keep)
        except (InstallError, OSError) as exc:
            log.warning("plugin %s: old version left for the next boot sweep: %s", plugin_id, exc)
    return _change(plugin_id, plugin, runtime, f"installed {manifest.name} {manifest.version}")


def _set_enabled(request: Request, plugin_id: str, enabled: bool) -> ChangeOut:
    runtime = _runtime(request)
    root = runtime.plugins_dir
    state = load_state(root)
    plugin = state.plugins.get(plugin_id)
    if plugin is None:
        raise _refuse(404, "not-installed", f"{plugin_id} is not installed")
    if enabled and not state.labs_opt_in and _is_labs(root, plugin):
        raise _refuse(409, "labs-not-enabled", "turn on the labs channel to enable a labs module")
    updated = InstalledPlugin(**{**plugin.__dict__, "enabled": enabled})
    _save(root, state.with_plugin(updated))
    verb = "enabled" if enabled else "disabled"
    return _change(plugin_id, updated, runtime, f"{plugin_id} {verb}")


@router.post("/{plugin_id}/enable", response_model=ChangeOut)
@_serialized
def enable(request: Request, plugin_id: str) -> ChangeOut:
    return _set_enabled(request, plugin_id, True)


@router.post("/{plugin_id}/disable", response_model=ChangeOut)
@_serialized
def disable(request: Request, plugin_id: str) -> ChangeOut:
    return _set_enabled(request, plugin_id, False)


@router.delete("/{plugin_id}", response_model=ChangeOut)
@_serialized
def uninstall(request: Request, plugin_id: str) -> ChangeOut:
    runtime = _runtime(request)
    root = runtime.plugins_dir
    state = load_state(root)
    if plugin_id not in state.plugins:
        raise _refuse(404, "not-installed", f"{plugin_id} is not installed")
    # Resident code (loaded, or a startup hook failed after registration) still
    # reads its folder until restart, so its version stays on disk.
    resident = runtime.hosts.get(plugin_id)
    keep = [resident.manifest.version] if resident is not None else []
    # Files first: if removal fails the record stays, so uninstall can be retried.
    try:
        remove_installed(root, plugin_id, keep=keep)
    except InstallError as exc:
        raise _refuse(409, exc.code, str(exc)) from exc
    except OSError as exc:
        raise _refuse(409, "remove-failed", f"could not remove {plugin_id}'s files: {exc}") from exc
    _save(root, state.without(plugin_id))
    return _change(plugin_id, None, runtime, f"{plugin_id} uninstalled")


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
    "dev-mode-off": 403, "labs-not-enabled": 409, "needs-newer-app": 409, "version-running": 409,
}


@router.post("/sideload/preview", response_model=SideloadPreviewOut)
def sideload_preview(body: SideloadPreviewIn) -> SideloadPreviewOut:
    """Show what a sideload asks consent for (permissions, terms) before it can be accepted."""
    try:
        manifest, digest = preview_directory(Path(body.path).expanduser())
    except InstallError as exc:
        raise _refuse(_SIDELOAD_STATUS.get(exc.code, 422), exc.code, str(exc)) from exc
    return SideloadPreviewOut(
        plugin_id=manifest.plugin_id, name=manifest.name, version=manifest.version,
        channel=manifest.channel, permissions=list(manifest.permissions), notice=manifest.notice,
        manifest_sha256=digest,
    )


@router.post("/sideload", response_model=ChangeOut)
@_serialized
def sideload(request: Request, body: SideloadIn) -> ChangeOut:
    runtime = _runtime(request)
    root = runtime.plugins_dir
    state = load_state(root)

    def consent(manifest: PluginManifest, digest: str) -> None:
        # A sideload gets no consent the store would not ask for (Codex on PR #6522).
        if digest != body.manifest_sha256:
            raise InstallError(
                "source-changed",
                f"{body.path} changed since it was previewed; preview it again before sideloading",
            )
        if manifest.channel == "labs" and not state.labs_opt_in:
            raise InstallError("labs-not-enabled", "turn on the labs channel before sideloading a labs module")
        if sorted(body.accept_permissions) != sorted(manifest.permissions):
            raise InstallError(
                "permissions-not-accepted",
                f"accept exactly these permissions to sideload: {list(manifest.permissions)}",
            )
        if manifest.needs_terms_notice and not body.acknowledge_notice:
            raise InstallError(
                "notice-not-acknowledged",
                f"preview this build and accept the terms it shows: {manifest.notice}",
            )
        check_app_version(manifest, _config(request).app_version)
        _refuse_replacing_running(runtime, manifest.plugin_id, manifest.version)
        consented.append(manifest)

    consented: list[PluginManifest] = []
    plugins: list[InstalledPlugin] = []

    def commit() -> None:
        # The record lands before any old copy of this version goes; a failure puts it back.
        manifest = consented[0]
        previous = state.plugins.get(manifest.plugin_id)
        plugin = InstalledPlugin(
            plugin_id=manifest.plugin_id,
            version=manifest.version,
            enabled=previous.enabled if previous is not None else True,  # an update never turns it back on
            source="sideload",
            installed_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
            granted_permissions=tuple(manifest.permissions),
            notice_acknowledged=manifest.needs_terms_notice and body.acknowledge_notice,
            accepted_notice_sha256=manifest.notice_sha256 if body.acknowledge_notice else "",
            install_id=install_id,
        )
        save_state(root, state.with_plugin(plugin))
        plugins.append(plugin)

    install_id = uuid.uuid4().hex
    try:
        manifest = install_directory(
            root, Path(body.path).expanduser(), consent=consent, then=commit, install_id=install_id,
        )
    except InstallError as exc:
        raise _refuse(_SIDELOAD_STATUS.get(exc.code, 422), exc.code, str(exc)) from exc
    except (OSError, StateFileError) as exc:  # a refused state write is put back like any failed write
        raise _refuse(409, "install-failed", f"could not sideload {body.path}: {exc}") from exc
    plugin = plugins[0]
    return _change(manifest.plugin_id, plugin, runtime, f"sideloaded {manifest.name} {manifest.version}")


@router.get("/ui-slots", response_model=list[UiSlotOut])
def ui_slots(request: Request) -> list[UiSlotOut]:
    runtime = _runtime(request)
    out: list[UiSlotOut] = []
    for plugin_id, manifest in sorted(runtime.manifests.items()):
        if manifest.ui is None or not runtime.loaded(plugin_id):
            continue
        # Each segment encoded: a '#' or '?' the manifest allows in a file name would end the path.
        entry = "/".join(quote(part, safe="") for part in manifest.ui.entry.split("/"))
        url = f"{API_PREFIX}/plugins/{plugin_id}/ui/{entry}?v={quote(manifest.version, safe='')}"
        out.extend(
            UiSlotOut(plugin_id=plugin_id, version=manifest.version, slot=slot, url=url)
            for slot in manifest.ui.slots
        )
    return out


@router.get("/{plugin_id}/ui/{path:path}")
def ui_file(request: Request, plugin_id: str, path: str) -> Response:
    runtime = _runtime(request)
    manifest = runtime.manifests.get(plugin_id)
    if manifest is None or manifest.ui is None or not runtime.loaded(plugin_id):
        raise _refuse(404, "not-loaded", f"{plugin_id} has no UI loaded this session")
    digest = manifest.files.get(path)
    if digest is None or not path.startswith(UI_DIR):
        raise _refuse(404, "not-listed", f"{path} is not a UI file of {plugin_id}")
    handle = _verified_handle(install_dir(runtime.plugins_dir, plugin_id, manifest.version) / path, path, digest)
    media = "text/javascript" if path.endswith((".js", ".mjs")) else (
        mimetypes.guess_type(path)[0] or "application/octet-stream"
    )
    return StreamingResponse(_chunks(handle), media_type=media, headers={"Cache-Control": "no-cache"})


def _verified_handle(target: Path, path: str, digest: str) -> BinaryIO:
    """A private snapshot of ``target``, hashed as it was copied, rewound: the bytes served are the bytes hashed.

    A UI file may be hundreds of MiB, so it is copied in chunks (spilling to a
    temp file past 1 MiB), never read whole; serving the original after hashing
    would serve a write made to it in between.
    """
    snapshot = tempfile.SpooledTemporaryFile(max_size=1 << 20)  # noqa: SIM115  (closed by _chunks or below)
    hasher, size = hashlib.sha256(), 0
    try:
        with open_regular(target) as handle:  # a FIFO or device swapped in must not hang this request
            if os.fstat(handle.fileno()).st_size > MAX_UNPACKED_BYTES:
                size = MAX_UNPACKED_BYTES + 1  # grown or replaced past any install: refused without reading it
            # No install holds more, so a file grown past the limit, even while read, is never copied whole.
            while size <= MAX_UNPACKED_BYTES and (chunk := handle.read(1 << 16)):
                size += len(chunk)
                hasher.update(chunk)
                snapshot.write(chunk)
    except OSError as exc:
        snapshot.close()
        raise _refuse(404, "missing", f"{path}: {exc}") from exc
    except InstallError as exc:
        snapshot.close()
        raise _refuse(409, "tampered", f"{path}: {exc}") from exc
    if size > MAX_UNPACKED_BYTES or hasher.hexdigest() != digest:
        snapshot.close()
        raise _refuse(409, "tampered", f"{path} changed after install")
    snapshot.seek(0)
    return cast(BinaryIO, snapshot)


def _chunks(handle: BinaryIO) -> Iterator[bytes]:
    with handle:
        while chunk := handle.read(1 << 16):
            yield chunk


@router.get("/{plugin_id}/media/{sha256}")
def listing_media(request: Request, plugin_id: str, sha256: str) -> Response:
    """A screenshot pinned by a visible signed listing, re-verified on every read."""
    state = load_state(_root(request))
    for channel in CHANNELS:
        if channel == "labs" and not state.labs_opt_in:
            continue
        read = _read(request, channel, refresh=False)
        entry = read.catalog.find(plugin_id) if read.catalog else None
        shot = next((s for s in entry.listing.screenshots if s.sha256 == sha256), None) if entry else None
        if shot is None:
            continue
        cfg = _config(request)
        try:
            data = read_screenshot(shot, _root(request) / "catalog" / "media",
                                   fetch=cfg.fetch, base_url=cfg.base_url)
        except CatalogError as exc:
            raise _refuse(502, exc.code, str(exc)) from exc
        return Response(content=data, media_type=shot.media_type,
                        headers={"Cache-Control": "max-age=86400, immutable"})
    raise _refuse(404, "not-listed", f"{sha256} is not a screenshot of {plugin_id} in a visible listing")
