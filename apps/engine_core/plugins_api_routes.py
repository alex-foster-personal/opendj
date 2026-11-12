"""Part of `plugins_api.py`, split out with no behavior change: routes."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from apps.engine_core.plugins_api_store import (
    _SIDELOAD_STATUS,
    API_PREFIX,
    SideloadPreviewIn,
    SideloadPreviewOut,
    UiSlotOut,
    _config,
    _read,
    _refuse,
    _root,
    _runtime,
    router,
)
from apps.plugins.catalog import (
    CatalogEntry,
    CatalogError,
    CatalogRead,
    read_screenshot,
)
from apps.plugins.installer import (
    MAX_UNPACKED_BYTES,
    InstallError,
    install_dir,
    open_regular,
    preview_directory,
)
from apps.plugins.manifest import CHANNELS, UI_DIR, semver_key
from apps.plugins.state import (
    load_state,
)
from apps.shared.semver import Semver


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


@router.post("/sideload/preview", response_model=SideloadPreviewOut)
def sideload_preview(body: SideloadPreviewIn) -> SideloadPreviewOut:
    """Show what a sideload asks consent for (permissions, terms) before it can be accepted."""
    try:
        manifest, digest = preview_directory(Path(body.path).expanduser())
    except InstallError as exc:
        raise _refuse(_SIDELOAD_STATUS.get(exc.code, 422), exc.code, str(exc)) from exc
    return SideloadPreviewOut(
        plugin_id=manifest.plugin_id,
        name=manifest.name,
        version=manifest.version,
        channel=manifest.channel,
        permissions=list(manifest.permissions),
        notice=manifest.notice,
        manifest_sha256=digest,
    )


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
            UiSlotOut(plugin_id=plugin_id, version=manifest.version, slot=slot, url=url) for slot in manifest.ui.slots
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
    media = (
        "text/javascript"
        if path.endswith((".js", ".mjs"))
        else (mimetypes.guess_type(path)[0] or "application/octet-stream")
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
            data = read_screenshot(shot, _root(request) / "catalog" / "media", fetch=cfg.fetch, base_url=cfg.base_url)
        except CatalogError as exc:
            raise _refuse(502, exc.code, str(exc)) from exc
        return Response(content=data, media_type=shot.media_type, headers={"Cache-Control": "max-age=86400, immutable"})
    raise _refuse(404, "not-listed", f"{sha256} is not a screenshot of {plugin_id} in a visible listing")
