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
    GET    /api/v1/plugins/cyanite/facts      published Cyanite pricing and limits
    GET    /api/v1/plugins/cyanite/ready      whether the Cyanite provider is configured
    POST   /api/v1/plugins/cyanite/enrich     Cyanite responses as V2 claim envelopes
    GET    /api/v1/plugins/epidemic-sound/ready   whether the Epidemic Sound provider is configured
    POST   /api/v1/plugins/epidemic-sound/enrich  Epidemic Sound responses as claim envelopes

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

import logging
import uuid
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Request

from apps.engine_core.plugins_api_routes import (  # noqa: F401
    _chunks,
    _newest_entries,
    _verified_handle,
    cyanite_enrich,
    cyanite_facts,
    cyanite_ready,
    epidemic_sound_enrich,
    epidemic_sound_ready,
    listing_media,
    sideload_preview,
    ui_file,
    ui_slots,
)
from apps.engine_core.plugins_api_store import (  # noqa: F401
    _SIDELOAD_STATUS,
    API_PREFIX,
    RUNTIME_STATE_ATTR,
    STORE_CONFIG_ATTR,
    ChangeOut,
    ChannelOut,
    CyaniteEnrichIn,
    CyaniteEnrichOut,
    CyaniteFactsOut,
    CyaniteReadyOut,
    EpidemicEnrichIn,
    EpidemicEnrichOut,
    EpidemicReadyOut,
    FaultOut,
    InstallIn,
    LabsIn,
    ListingOut,
    P,
    PluginRowOut,
    PluginsOut,
    ProviderQuotaOut,
    ProviderSurfaceOut,
    R,
    ScreenshotOut,
    SideloadIn,
    SideloadPreviewIn,
    SideloadPreviewOut,
    StoreConfig,
    UiSlotOut,
    _channel_out,
    _check_consent,
    _clearable_reads,
    _config,
    _consented_entry,
    _describes,
    _installed_manifest,
    _iso,
    _listing_out,
    _note_verified,
    _offers_update,
    _provider_out,
    _read,
    _rebuilt,
    _refuse,
    _refuse_replacing_running,
    _require_loaded,
    _root,
    _runtime,
    _save,
    _seen,
    _serialized,
    _shown_manifest,
    router,
)
from apps.plugins.catalog import (
    CatalogCache,
    CatalogEntry,
    CatalogError,
    CatalogRead,
    read_channel,
)
from apps.plugins.host import PluginRuntime, load_enabled, taken_routes
from apps.plugins.installer import (
    MAX_ARCHIVE_BYTES,
    InstallError,
    check_app_version,
    dev_mode,
    install_archive,
    install_directory,
    receipt_written,
    recover_replacements,
    remove_installed,
    sweep_unrecorded,
)
from apps.plugins.manifest import CHANNELS, PluginManifest
from apps.plugins.state import (
    InstalledPlugin,
    PluginState,
    StateFileError,
    load_state,
    plugins_dir,
    save_state,
)

log = logging.getLogger(__name__)


# ----- boot -----------------------------------------------------------------
def mount_plugins(app: FastAPI, data_dir: Path, *, config: StoreConfig | None = None) -> PluginRuntime:
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
    reads = (
        [read_channel(channel, cache, refresh=False, trusted=cfg.trusted) for channel in CHANNELS]
        if state.plugins
        else []
    )
    catalogs = [read.catalog for read in reads if read.catalog is not None]
    if reads:
        _note_verified(root, reads)
        state = load_state(root)
    # The store's own routes are reserved before any plugin claims a path, or a
    # module declaring /plugins would be mounted ahead of them and shadow them.
    taken = taken_routes(app.routes) + taken_routes(router.routes, API_PREFIX)
    runtime = load_enabled(
        root,
        data_dir,
        state,
        catalogs=catalogs,
        taken=taken,
        trusted=cfg.trusted,
        app_version=cfg.app_version,
        held=held,
    )
    for plugin_id, host in runtime.hosts.items():
        for plugin_router in host.routers:
            app.include_router(plugin_router, prefix=API_PREFIX, dependencies=[Depends(_require_loaded(plugin_id))])
    for outcome in runtime.outcomes.values():
        level = logging.INFO if outcome.status == "loaded" else logging.WARNING
        log.log(level, "plugin %s %s: %s %s", outcome.plugin_id, outcome.version, outcome.status, outcome.detail)
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
    if (
        installed.enabled
        and outcome is not None
        and (outcome.version != installed.version or runtime.install_ids.get(plugin_id) != installed.install_id)
    ):
        return "update"  # a different version, or the same version reinstalled since this boot
    return None


def _row(
    manifest: PluginManifest,
    available: CatalogEntry | None,
    installed: InstalledPlugin | None,
    runtime: PluginRuntime,
) -> PluginRowOut:
    outcome = runtime.outcomes.get(manifest.plugin_id)
    update = available is not None and installed is not None and _offers_update(available, installed, runtime)
    provider_surface = runtime.providers.surface(manifest.plugin_id, manifest.provider)
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
        provider=_provider_out(provider_surface),
    )


def _is_labs(root: Path, plugin: InstalledPlugin) -> bool:
    """A labs module, whether it came from the labs channel or was sideloaded."""
    if plugin.source == "store:labs":
        return True
    manifest = _installed_manifest(root, plugin) if plugin.source == "sideload" else None
    return manifest is not None and manifest.channel == "labs"


def _change(plugin_id: str, plugin: InstalledPlugin | None, runtime: PluginRuntime, message: str) -> ChangeOut:
    pending = _pending(plugin, runtime, plugin_id)
    return ChangeOut(
        plugin_id=plugin_id,
        version=plugin.version if plugin else None,
        enabled=bool(plugin and plugin.enabled),
        restart_required=pending is not None,
        message=message,
    )


def _rows(reads: Iterable[CatalogRead], state: PluginState, runtime: PluginRuntime) -> dict[str, PluginRowOut]:
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
        provider=None,
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
                    409,
                    "labs-module-enabled",
                    f"disable {plugin.plugin_id} before leaving the labs channel",
                )
    _save(root, replace(state, labs_opt_in=body.opt_in))
    return list_plugins(request)


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
            root,
            Path(body.path).expanduser(),
            consent=consent,
            then=commit,
            install_id=install_id,
        )
    except InstallError as exc:
        raise _refuse(_SIDELOAD_STATUS.get(exc.code, 422), exc.code, str(exc)) from exc
    except (OSError, StateFileError) as exc:  # a refused state write is put back like any failed write
        raise _refuse(409, "install-failed", f"could not sideload {body.path}: {exc}") from exc
    plugin = plugins[0]
    return _change(manifest.plugin_id, plugin, runtime, f"sideloaded {manifest.name} {manifest.version}")
