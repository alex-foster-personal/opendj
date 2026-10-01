"""What one rekordbox-mapped listing row reads off the share root (LIBM-137).

A row's preview strip, PVDI vocal regions and cover-art verdict all come from
files under the Pioneer share root. This module reads the three together and
remembers the result, so a warm listing page costs a few ``lstat`` calls a row
instead of a containment walk per file.

WHAT IS REMEMBERED, AND WHY THAT IS NOT A CONTAINMENT CACHE. The cross-request
cache rejected on review (see ``platform_paths.AssetResolver``) kept a "safe"
verdict and a resolved path, and a later request opened that path without
rerunning the check. Nothing here keeps a path or a verdict:

* The bytes a remembered value was derived from were read THROUGH the
  descriptors of the containment walk itself (``fd_anchored_walk.read_leaf``):
  root identity-checked, every directory opened ``O_NOFOLLOW`` relative to its
  parent's descriptor, the file opened ``O_NOFOLLOW`` relative to its
  directory's descriptor and read from that descriptor. No path is reopened, so
  a swap during the read cannot put outside bytes into the value.
* A row is remembered only when nothing on its paths was refused. A symlink, an
  unreadable directory, an oversized file or a changed root yields this
  request's answer and no entry.
* A hit opens nothing. It is served only while the root still has the identity
  it had, every directory on the row's paths is still a real directory with the
  same inode, and every file the read looked at still has the same type, inode,
  device, size, modification time and change time. Any mismatch, and any error
  while checking, drops the entry and reads the row again through the walk.

The demucs vocal fallback is deliberately NOT part of the remembered value: a
vocal-cache entry landing later must show on the next page, so callers consult
it live on every call.

MEMORY. One entry is two vendor path strings, a 16-byte digest of what every
path on the row looked like, the preview strip as base64 and the vocal regions
as JSON text. The measured figure is in
``ops/perf/library-listing-boot-round-3/report.md``. The cache is
least-recently-used and holds at most :data:`ROW_ASSET_CACHE_MAX_ENTRIES` rows.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import TracebackType
from typing import Any

from apps.adapters.rekordbox import config
from apps.adapters.rekordbox import paths as rb_paths
from apps.adapters.rekordbox.models import RbRowMeta
from apps.shared import fd_anchored_walk, platform_paths
from apps.shared.platform_paths import AssetResolver

from . import anlz
from .row_hydration_cache import (
    _ABSENT,
    RootKey,
    RowAssetCache,
    RowValue,
    digest,
    directory_witness,
    file_witness,
    lstat_below,
)

log = logging.getLogger(__name__)

#: Most rows the cache holds: four times the 10,000-track library the listing
#: is budgeted for, so a full walk of it never evicts. Least recently used goes
#: first. The memory this bounds is measured in the round 3 report.
ROW_ASSET_CACHE_MAX_ENTRIES: int = 40_000

#: Largest analysis file read into memory. Real ANLZ files are under 2 MB.
MAX_ASSET_BYTES: int = 64 * 1024 * 1024

_PREVIEW_SUFFIXES: tuple[str, ...] = tuple(suffix for suffix, _reader in anlz._PREVIEW_SOURCES)
_NOT_ANALYZED: str = sys.intern('{"status": "not_analyzed"}')


@dataclass(frozen=True)
class RowAssets:
    """The share-root facts of one mapped row."""

    preview_b64: str | None
    preview_max: int | None
    artwork_available: bool | None
    artwork_status: str
    #: PVDI vocals only (rekordbox / no_vocals / not_analyzed), before the
    #: demucs fallback. A fresh object per caller: rows go to serializers.
    pvdi_vocals: dict[str, Any]


@dataclass(frozen=True)
class _ReadBytes:
    """Bytes already read through the walk, shaped like the path the readers take."""

    label: str
    data: bytes

    def read_bytes(self) -> bytes:
        return self.data

    def __str__(self) -> str:
        return self.label


# ----- the plan: which paths a row's vendor strings name ----------------------


@dataclass(frozen=True)
class _Plan:
    analysis_dirs: tuple[str, ...]
    #: The leaf the vendor path names, "" for a row with no analysis path.
    analysis_leaf: str
    #: The preview chain's file names, in ``anlz._PREVIEW_SOURCES`` order.
    analysis_candidates: tuple[str, ...]
    #: Every analysis leaf an entry answers for: the two above, deduplicated.
    analysis_leaves: tuple[str, ...]
    artwork_dirs: tuple[str, ...]
    artwork_leaves: tuple[str, ...]

    def witnessed(self) -> list[tuple[str, bool]]:
        """Every ``(share-relative path, is_directory)`` an entry answers for."""
        out: list[tuple[str, bool]] = []
        for dirs, leaves in (
            (self.analysis_dirs, self.analysis_leaves),
            (self.artwork_dirs, self.artwork_leaves),
        ):
            out.extend(("/".join(dirs[:depth]), True) for depth in range(1, len(dirs) + 1))
            out.extend(("/".join((*dirs, leaf)), False) for leaf in leaves)
        return out


def _share_parts(vendor_path: str) -> tuple[str, ...] | None:
    """Components of a plain share-relative vendor path, None for anything else.

    Only ``/PIONEER/...`` maps onto the share root without consulting the path
    map or the filesystem (``platform_paths.resolve_library_path``), so only
    that form can be keyed on its own string. Anything unusual in it (an empty
    component, ``.``, ``..``, a NUL) is left to the uncached resolver.
    """
    if not vendor_path.startswith("/PIONEER/") or "\x00" in vendor_path:
        return None
    parts = tuple(vendor_path.split("/")[1:])
    if len(parts) < 2 or any(part in ("", ".", "..") for part in parts):
        return None
    return parts


def _plan_for(meta: RbRowMeta) -> _Plan | None:
    """The row's paths, or None when the row must take the uncached path."""
    if not fd_anchored_walk.FD_ANCHORED_WALK_SUPPORTED:
        return None
    analysis_dirs: tuple[str, ...] = ()
    analysis_leaf = ""
    analysis_candidates: tuple[str, ...] = ()
    if meta.analysis_data_path:
        parts = _share_parts(meta.analysis_data_path)
        if parts is None:
            return None
        leaf = PurePosixPath(parts[-1])
        try:
            analysis_candidates = tuple(
                leaf.with_suffix(suffix).name for suffix in _PREVIEW_SUFFIXES
            )
        except ValueError:
            return None
        analysis_dirs, analysis_leaf = parts[:-1], parts[-1]
    artwork_dirs: tuple[str, ...] = ()
    artwork_leaves: tuple[str, ...] = ()
    if meta.image_path is not None:
        parts = _share_parts(meta.image_path)
        if parts is None:
            return None
        artwork_dirs = parts[:-1]
        artwork_leaves = tuple(dict.fromkeys((parts[-1], config.ARTWORK_FILENAMES["s"])))
    analysis_leaves = tuple(dict.fromkeys((analysis_leaf, *analysis_candidates))) if analysis_leaf else ()
    return _Plan(
        analysis_dirs, analysis_leaf, analysis_candidates, analysis_leaves,
        artwork_dirs, artwork_leaves,
    )


_ROW_ASSETS: RowAssetCache = RowAssetCache(ROW_ASSET_CACHE_MAX_ENTRIES)
platform_paths.on_share_root_refresh(_ROW_ASSETS.clear)


# ----- one caller's pass over its rows ---------------------------------------


class _Refused(Exception):
    """Something on the row's path was refused: answer, but remember nothing."""


class _Leaves:
    """The files of one walked directory: read through its descriptor, each
    one's identity noted, and the first refusal remembered."""

    def __init__(self, dir_fd: int, prefix: str, seen: dict[str, bytes]) -> None:
        self._dir_fd: int = dir_fd
        self._prefix: str = prefix
        self._seen: dict[str, bytes] = seen
        self._data: dict[str, bytes] = {}
        self.refused: str | None = None

    def _note(self, name: str, st: os.stat_result | None) -> None:
        self._seen[f"{self._prefix}/{name}"] = _ABSENT if st is None else file_witness(st)

    def lstat(self, name: str) -> os.stat_result | None:
        st = fd_anchored_walk.stat_leaf(self._dir_fd, name)
        self._note(name, st)
        return st

    def witness_unread(self, names: tuple[str, ...]) -> None:
        """Note every name the chain never reached, so a file appearing there
        later is a change."""
        for name in names:
            if f"{self._prefix}/{name}" not in self._seen and self.refused is None:
                try:
                    self.lstat(name)
                except OSError as exc:
                    self.refused = f"{name}: {exc}"

    def read(self, name: str) -> bytes | None:
        """The file's bytes; None when it is absent, not a regular file, or refused."""
        if name in self._data:
            return self._data[name]
        try:
            got = fd_anchored_walk.read_leaf(self._dir_fd, name, MAX_ASSET_BYTES)
        except OSError as exc:
            self.refused = f"{name}: {exc}"
            return None
        if got is None:
            self._note(name, None)
            return None
        st, payload = got
        self._note(name, st)
        if payload is None:
            if stat.S_ISREG(st.st_mode):
                self.refused = f"{name}: larger than {MAX_ASSET_BYTES} bytes"
            return None
        self._data[name] = payload
        return payload


class _RefusedWith(_Refused):
    """A sibling was refused after the rest of the chain produced an answer."""

    def __init__(self, why: str, value: tuple[str | None, int | None, str]) -> None:
        super().__init__(why)
        self.value = value


class RowAssetSession:
    """One bulk caller's pass: the root is opened and identity-checked once.

    Holds the root's descriptor for the length of the pass, so every row is
    walked from the same anchored directory, and memoizes the directory
    ``lstat`` results rows share. Nothing in it outlives the pass.
    """

    def __init__(self, resolver: AssetResolver, cache: RowAssetCache = _ROW_ASSETS) -> None:
        self._resolver: AssetResolver = resolver
        self._cache: RowAssetCache = cache
        self._root_path = platform_paths.SHARE_ROOT
        self._root_fd: int | None = None
        #: "unopened", "open", "missing" or "changed".
        self._root_state: str = "unopened"
        self._root_key: RootKey | None = None
        self._directory_witnesses: dict[str, bytes | None] = {}

    def __enter__(self) -> RowAssetSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    def _open_root(self) -> None:
        if self._root_state != "unopened":
            return
        try:
            self._root_fd = fd_anchored_walk.open_anchored_root(self._root_path)
        except fd_anchored_walk.RootIdentityChanged as exc:
            fd_anchored_walk.log_root_identity_changed(exc)
            self._root_state = "changed"
            return
        except OSError:
            self._root_state = "changed"
            return
        if self._root_fd is None:
            self._root_state = "missing"
            return
        anchored = os.fstat(self._root_fd)
        self._root_key = (str(self._root_path), anchored.st_dev, anchored.st_ino)
        self._root_state = "open"

    # -- warm --

    def _current_witnesses(self, plan: _Plan) -> bytes | None:
        """What the row's paths name right now; None when any cannot be told."""
        assert self._root_fd is not None
        out: list[bytes] = []
        for relative, is_directory in plan.witnessed():
            if is_directory:
                if relative not in self._directory_witnesses:
                    st = lstat_below(self._root_fd, relative)
                    self._directory_witnesses[relative] = (
                        None if isinstance(st, OSError)
                        else _ABSENT if st is None
                        else directory_witness(st)
                    )
                witness = self._directory_witnesses[relative]
                if witness is None:
                    return None
                out.append(witness)
                continue
            st = lstat_below(self._root_fd, relative)
            if isinstance(st, OSError):
                return None
            out.append(_ABSENT if st is None else file_witness(st))
        return digest(out)

    # -- cold --

    def _read_analysis(
        self, plan: _Plan, seen: dict[str, bytes]
    ) -> tuple[str | None, int | None, str]:
        """Preview strip and PVDI vocals JSON, read through the walk."""
        assert self._root_fd is not None
        dirs = plan.analysis_dirs
        try:
            dir_fd, stats = fd_anchored_walk.open_directory_under(self._root_fd, dirs)
        except OSError as exc:
            raise _Refused(f"analysis directory: {exc}") from exc
        for depth, st in enumerate(stats, start=1):
            seen["/".join(dirs[:depth])] = directory_witness(st)
        if dir_fd is None:
            return None, None, _NOT_ANALYZED
        try:
            return self._read_analysis_leaves(plan, dir_fd, seen)
        finally:
            os.close(dir_fd)

    def _read_analysis_leaves(
        self, plan: _Plan, dir_fd: int, seen: dict[str, bytes]
    ) -> tuple[str | None, int | None, str]:
        prefix = "/".join(plan.analysis_dirs)
        leaves = _Leaves(dir_fd, prefix, seen)
        try:
            vendor_st = leaves.lstat(plan.analysis_leaf)
        except OSError as exc:
            raise _Refused(f"{plan.analysis_leaf}: {exc}") from exc
        if vendor_st is not None and stat.S_ISLNK(vendor_st.st_mode):
            raise _Refused(f"{plan.analysis_leaf}: is a symlink")

        label = f"{self._root_path}/{prefix}"
        candidates = plan.analysis_candidates
        preview: tuple[str | None, int | None] = (None, None)
        for (_suffix, reader), name in zip(anlz._PREVIEW_SOURCES, candidates, strict=True):
            payload = leaves.read(name)
            cols = None if payload is None else reader(_ReadBytes(f"{label}/{name}", payload))
            if cols is not None:
                preview = anlz.encode_preview_strip(cols)
                break
        else:
            log.warning("preview_strip: no preview in PWV6/PWV4/PWAV for %s", prefix)

        twoex = leaves.read(candidates[0])
        vocals = _NOT_ANALYZED
        if twoex is not None:
            source = _ReadBytes(f"{label}/{candidates[0]}", twoex)
            vocals = json.dumps(anlz._decode_vocals_payload(source))
        leaves.witness_unread(candidates)
        if leaves.refused is not None:
            raise _RefusedWith(leaves.refused, (*preview, vocals))
        return (*preview, vocals)

    def _read_artwork(self, plan: _Plan, seen: dict[str, bytes]) -> tuple[bool | None, str]:
        """The cover verdict. Nothing is read: both leaves are ``lstat``-ed
        through the walked directory, so a symlink there is seen and refused."""
        assert self._root_fd is not None
        dirs = plan.artwork_dirs
        try:
            dir_fd, stats = fd_anchored_walk.open_directory_under(self._root_fd, dirs)
        except OSError as exc:
            raise _Refused(f"artwork directory: {exc}") from exc
        for depth, st in enumerate(stats, start=1):
            seen["/".join(dirs[:depth])] = directory_witness(st)
        if dir_fd is None:
            return False, "file_missing"
        prefix = "/".join(dirs)
        try:
            found: list[os.stat_result | None] = []
            for name in plan.artwork_leaves:
                st = fd_anchored_walk.stat_leaf(dir_fd, name)
                if st is not None and stat.S_ISLNK(st.st_mode):
                    raise _Refused(f"{name}: is a symlink")
                seen[f"{prefix}/{name}"] = _ABSENT if st is None else file_witness(st)
                found.append(st)
        except OSError as exc:
            raise _Refused(f"artwork: {exc}") from exc
        finally:
            os.close(dir_fd)
        small = found[-1]
        if small is None or not stat.S_ISREG(small.st_mode):
            return False, "file_missing"
        return True, "ok"

    def _read(self, plan: _Plan) -> tuple[RowValue, bytes | None]:
        """Read one row through the walk: its value, and its witnesses when
        nothing was refused (None means "do not remember this")."""
        seen: dict[str, bytes] = {}
        cacheable = True
        preview_b64: str | None = None
        preview_max: int | None = None
        vocals = _NOT_ANALYZED
        if plan.analysis_leaves:
            try:
                preview_b64, preview_max, vocals = self._read_analysis(plan, seen)
            except _RefusedWith as partial:
                cacheable = False
                preview_b64, preview_max, vocals = partial.value
            except _Refused:
                cacheable = False
        artwork: tuple[bool | None, str] = (False, "no_image_path")
        if plan.artwork_leaves:
            try:
                artwork = self._read_artwork(plan, seen)
            except _Refused:
                cacheable = False
                artwork = (False, "unresolved")
        value: RowValue = (preview_b64, preview_max, *artwork, sys.intern(vocals))
        if not cacheable:
            return value, None
        return value, digest([seen.get(path, _ABSENT) for path, _is_dir in plan.witnessed()])

    # -- the row --

    def assets(self, meta: RbRowMeta) -> RowAssets:
        """Preview strip, PVDI vocals and artwork verdict of one mapped row."""
        plan = _plan_for(meta)
        if plan is None:
            return _uncached_row_assets(meta, self._resolver)
        self._open_root()
        if self._root_state == "missing":
            artwork = "file_missing" if plan.artwork_leaves else "no_image_path"
            return _row_assets((None, None, False, artwork, _NOT_ANALYZED))
        if self._root_state == "changed" or self._root_key is None:
            artwork = "unresolved" if plan.artwork_leaves else "no_image_path"
            return _row_assets((None, None, False, artwork, _NOT_ANALYZED))
        key = (meta.analysis_data_path or "", meta.image_path or "")
        hit = self._cache.get(self._root_key, key)
        if hit is not None:
            if hit[0] == self._current_witnesses(plan):
                return _row_assets(hit[1])
            self._cache.discard(self._root_key, key)
        value, witnesses = self._read(plan)
        if witnesses is not None:
            self._cache.put(self._root_key, key, witnesses, value)
        return _row_assets(value)


def _row_assets(value: RowValue) -> RowAssets:
    preview_b64, preview_max, artwork_available, artwork_status, vocals = value
    return RowAssets(preview_b64, preview_max, artwork_available, artwork_status, json.loads(vocals))


# ----- the uncached path: anything that is not a plain share-relative row -----


def pvdi_vocals(
    analysis_data_path: str | None, *, resolver: AssetResolver | None = None
) -> dict[str, Any]:
    """The PVDI ``vocals`` payload from a row's .2EX, ``not_analyzed`` without one."""
    if analysis_data_path:
        mapped = rb_paths.resolve_asset_path(analysis_data_path, resolver=resolver)
        if mapped.resolved is not None:
            twoex = rb_paths._asset_sibling(
                mapped, mapped.resolved.with_suffix(".2EX"), resolver=resolver
            )
            if twoex.resolved is not None:
                return anlz.vocals_payload(twoex.resolved)
    return {"status": "not_analyzed"}


def rb_artwork_facts(
    meta: RbRowMeta, *, resolver: AssetResolver | None = None
) -> tuple[bool | None, str]:
    """Wire pair for a rekordbox-mapped row: its pre-rendered small cover.

    The cover itself goes through the containment walk, as the artwork route's
    own lookup does, so a cover that is a symlink reads as unresolved here too.
    """
    if meta.image_path is None:
        return False, "no_image_path"
    mapped = rb_paths.resolve_asset_path(meta.image_path, resolver=resolver)
    if mapped.resolved is None:
        return False, "unresolved"
    small = rb_paths._asset_sibling(
        mapped, mapped.resolved.parent / config.ARTWORK_FILENAMES["s"], resolver=resolver
    )
    if small.resolved is None:
        return False, "unresolved"
    if not small.resolved.is_file():
        return False, "file_missing"
    return True, "ok"


def _uncached_row_assets(meta: RbRowMeta, resolver: AssetResolver) -> RowAssets:
    preview_b64, preview_max = anlz.preview_strip(meta.analysis_data_path, resolver=resolver)
    artwork_available, artwork_status = rb_artwork_facts(meta, resolver=resolver)
    return RowAssets(
        preview_b64, preview_max, artwork_available, artwork_status,
        pvdi_vocals(meta.analysis_data_path, resolver=resolver),
    )


def rb_row_assets(meta: RbRowMeta, *, resolver: AssetResolver) -> RowAssets:
    """One row on its own. A bulk caller uses :func:`bulk_rb_row_assets`."""
    with RowAssetSession(resolver) as session:
        return session.assets(meta)


def bulk_rb_row_assets(
    metas: dict[str, RbRowMeta], *, resolver: AssetResolver
) -> dict[str, RowAssets]:
    """Assets for every mapped row of one bulk pass, keyed like ``metas``.

    ``resolver`` is the caller's per-call memo (pin ad59ac); only rows that are
    not plain share-relative paths still go through it.
    """
    with RowAssetSession(resolver) as session:
        return {stable_id: session.assets(meta) for stable_id, meta in metas.items()}
