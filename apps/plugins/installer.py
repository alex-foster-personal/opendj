"""Unpack a plugin into ``<plugins-dir>/<id>/<version>/``, verified file by file.

Two ways in, one verification path:

* :func:`install_archive`: a zip downloaded from the URL a signed catalog
  entry names. The archive's sha256 must equal the signed ``archive_sha256``
  and its manifest must equal the signed manifest (PLUG-6522b).
* :func:`install_directory`: developer sideload from a local build directory,
  allowed only while ``MDT_PLUGIN_DEV=1`` (PLUG-6522g). Recorded as source
  ``sideload`` so the store labels it unsigned.

Either way the set of files must equal the manifest's ``files`` exactly
(nothing extra, nothing missing) with matching sha256, paths may not escape
the plugin directory, zip symlinks are refused, and the result is staged
then moved into place with ``os.replace`` so a failed install leaves nothing
half-written. :func:`verify_installed` repeats the hash check at every boot,
so a file edited after install refuses to load (PLUG-6522c).

The hashes it checks come from the installed manifest, which sits in the same
writable directory, so a store install also keeps the signed index it came
from as a receipt (``<plugins-dir>/receipts/<id>/<version>.index.json`` and
its ``.sig``). :func:`verify_receipt` re-checks that signature at boot and
requires the installed manifest to equal the signed one, so editing a file
and its manifest together still refuses to load.

Requirements (mini-PRD):
  * ✔︎ ✅ 🎯 an archive whose hash differs from the signed entry is refused
    before it is opened. [if] an unsigned archive is unpacked [then ⛔️]
  * ✔︎ ✅ 🎯 an extra file, a missing file or a changed byte is refused,
    naming the path. [if] a planted file installs [then ⛔️]
  * ✔︎ ✅ 🎯 a zip-slip path or a symlink entry is refused.
  * ✔︎ ✅ 🎯 a file changed after install fails verify_installed.
  * ✔︎ ✅ 🎯 a store install whose manifest no longer matches its signed
    receipt fails verify_receipt. [if] editing a file and its manifest
    together loads unsigned code [then ⛔️]
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import json
import os
import shutil
import stat
import uuid
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from apps.plugins.catalog import MAX_INDEX_BYTES, MAX_SIGNATURE_BYTES, CatalogEntry, CatalogError, parse_index
from apps.plugins.manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    PluginManifest,
    check_archive_path,
    is_plugin_id,
    parse_manifest,
    semver_key,
)
from apps.plugins.receipts import (
    RECEIPTS_DIRNAME,
    manifest_bytes as read_manifest_bytes,
    read_capped,
    receipt_paths,
)
from apps.plugins.regular import NotRegularFileError, open_regular_file
from apps.plugins.state import STATE_FILENAME, write_atomic
from apps.shared.semver import parse_semver

DEV_ENV: str = "MDT_PLUGIN_DEV"
MAX_ARCHIVE_BYTES: int = 256 * 1024 * 1024
MAX_UNPACKED_BYTES: int = 512 * 1024 * 1024
STAGING_DIRNAME: str = ".staging"
#: An install id is uuid4().hex (32 characters); a bigger ``.txn`` marker is not one.
_MAX_TOKEN_BYTES = 64
#: A receipt's signature file is held to the cap the catalog fetch applies.
_MAX_SIGNATURE_BYTES = MAX_SIGNATURE_BYTES


class InstallError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def dev_mode(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(DEV_ENV) == "1"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path, limit: int) -> tuple[str, int] | None:
    """The file's sha256 and size, read in 1 MiB chunks, or None once it passes ``limit`` bytes.

    An installed file can be replaced by one of any size after install, and
    boot hashes it before the engine starts, so a huge one is never read whole.
    """
    digest, size = hashlib.sha256(), 0
    with open_regular_file(path) as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                return None
            digest.update(chunk)
    return digest.hexdigest(), size


def install_dir(root: Path, plugin_id: str, version: str) -> Path:
    return root / plugin_id / version


def _contained(root: Path, path: Path) -> Path:
    """``path`` under ``root``, refused when any part below ``root`` is a symlink.

    The installer writes and deletes here; through a planted or stale link it
    would write or ``rmtree`` outside the plugins folder.
    """
    if root.is_symlink():  # every path below would resolve beneath the link's target and pass the check
        raise InstallError("unsafe-path", f"{root} is a symlink; refusing to write or delete through it")
    probe = path
    while probe != root:
        if probe.is_symlink():
            raise InstallError("unsafe-path", f"{probe} is a symlink; refusing to write or delete through it")
        if probe == probe.parent:
            raise InstallError("unsafe-path", f"{path} is not inside {root}")
        probe = probe.parent
    if not path.resolve().is_relative_to(root.resolve()):
        raise InstallError("unsafe-path", f"{path} resolves outside {root}")
    return path


#: Opens one payload file (a zip member or a sideload source file); read in chunks, never whole.
Opener = Callable[[], IO[bytes]]


def _check_listing(manifest: PluginManifest, present: Iterable[str]) -> None:
    listed = set(manifest.files)
    extra = sorted(set(present) - listed)
    missing = sorted(listed - set(present))
    if extra:
        raise InstallError("unlisted-file", f"file(s) not listed in the manifest: {extra[:5]}")
    if missing:
        raise InstallError("missing-file", f"file(s) listed but absent: {missing[:5]}")


def _streamed(
    manifest: PluginManifest,
    rel: str,
    opener: Opener,
    sink: IO[bytes] | None = None,
    *,
    limit: int = MAX_UNPACKED_BYTES,
) -> int:
    """Hash ``rel`` in chunks (copying them to ``sink``) and return its size.

    Raises 'too-large' once it passes ``limit`` bytes, so a file that grows
    while it is read never fills the disk, and 'hash-mismatch' unless it is
    the listed file.
    """
    hasher, size = hashlib.sha256(), 0
    with opener() as handle:
        while chunk := handle.read(1 << 16):
            size += len(chunk)
            if size > limit:
                raise InstallError("too-large", f"{rel} passes the {MAX_UNPACKED_BYTES}-byte unpacked limit")
            hasher.update(chunk)
            if sink is not None:
                sink.write(chunk)
    if hasher.hexdigest() != manifest.files[rel]:
        raise InstallError("hash-mismatch", f"{rel} does not match its sha256 in the manifest")
    return size


def _write_staged(
    root: Path,
    manifest: PluginManifest,
    manifest_bytes: bytes,
    payload: Mapping[str, Opener],
    then: Callable[[], None] | None,
    install_id: str | None,
) -> Path:
    staging = _contained(root, root / STAGING_DIRNAME / f"{manifest.plugin_id}-{uuid.uuid4().hex}")
    staging.mkdir(parents=True)
    try:
        (staging / MANIFEST_FILENAME).write_bytes(manifest_bytes)
        left = MAX_UNPACKED_BYTES  # boot refuses a bigger payload, so no install may commit one
        for rel, opener in payload.items():  # verified as it is written, so the bytes staged are the bytes hashed
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as sink:
                left -= _streamed(manifest, rel, opener, sink, limit=left)
        final = _contained(root, install_dir(root, manifest.plugin_id, manifest.version))
        final.parent.mkdir(parents=True, exist_ok=True)
        _contained(root, final)  # the parent just made, or one that was already there
        _commit(root, staging, final, then, install_id)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final


def _aside(root: Path, final: Path, suffix: str) -> Path:
    """A path in the staging folder that belongs to one version's replacement."""
    plugin_id, version = final.parent.name, final.name
    return _contained(root, root / STAGING_DIRNAME / f"{plugin_id}@{version}.{suffix}")


def _previous(root: Path, final: Path) -> Path:
    """The old copy while its replacement lands; its presence means the replacement is open.

    Empty when there was no old copy, so an open replacement always leaves this
    folder for :func:`recover_replacements` to find.
    """
    return _aside(root, final, "previous")


def _receipt_backup(root: Path, final: Path) -> Path:
    """The receipt files a replacement overwrote, copied here first so a crash can put them back."""
    return _aside(root, final, "receipt")


def _rollback(root: Path, final: Path) -> None:
    """Undo an open replacement: the old copy and the old receipt go back, the new ones go.

    The receipt goes back first: ``.previous`` marks the replacement open until the
    files are back too, so a crash part way through is rolled back again at boot.
    """
    _restore_receipt(root, final)
    previous = _previous(root, final)
    if final.exists():
        shutil.rmtree(final)
    if any(previous.iterdir()):
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(previous, final)
    else:
        previous.rmdir()  # there was no old copy to put back


def _restore_receipt(root: Path, final: Path) -> None:
    backup = _receipt_backup(root, final)
    if not backup.is_dir():
        return
    paths = receipt_paths(root, final.parent.name, final.name)
    # A plain file in a receipt folder's place would stop this restore part way and strand the old copy.
    folder = _receipt_folder(root, final.parent.name)
    for path, limit in zip(paths, (MAX_INDEX_BYTES, _MAX_SIGNATURE_BYTES), strict=True):
        saved = backup / path.name
        if path.is_dir() and not path.is_symlink():
            _remove_path(path)  # a folder in a receipt file's place would stop the restore part way
        # A saved copy past its cap was never written by this installer and could never verify.
        data = read_capped(saved, limit) if saved.is_file() else None
        if data is not None:
            folder.mkdir(parents=True, exist_ok=True)
            write_atomic(path, data)
        else:
            path.unlink(missing_ok=True)  # the replacement added it; there was none before
    shutil.rmtree(backup)


def _finish(root: Path, final: Path) -> None:
    """Drop what a committed replacement kept aside."""
    shutil.rmtree(_receipt_backup(root, final), ignore_errors=True)
    with contextlib.suppress(OSError):  # a locked marker is cleared before the next replacement
        _aside(root, final, "txn").unlink(missing_ok=True)
    shutil.rmtree(_aside(root, final, "done"), ignore_errors=True)


def _clear_leftovers(root: Path, final: Path) -> None:
    """Remove what an earlier, committed replacement left aside, or refuse to start a new one.

    :func:`_finish` ignores errors so a committed install is never reported as
    failed. A receipt backup it could not remove would then be taken as this
    replacement's own, and a rollback would put that stale receipt back.
    """
    previous = _previous(root, final)
    stray = [previous] if previous.exists() and not previous.is_dir() else []  # a file marks no open replacement
    for path in (*stray, _receipt_backup(root, final), _aside(root, final, "txn"), _aside(root, final, "done")):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError as exc:
            raise InstallError(
                "cleanup-pending",
                f"could not clear {path.name} from an earlier install; restart the engine first ({exc})",
            ) from exc


def _txn_token(txn: Path) -> str:
    """The install id a replacement wrote to its ``.txn`` marker, or "" for none.

    A marker that is not a short ASCII token cannot match any install record, so
    it reads as "no token" and the replacement is rolled back, never a boot crash.
    """
    if not txn.is_file():
        return ""
    try:
        with open_regular_file(txn) as handle:
            raw = handle.read(_MAX_TOKEN_BYTES + 1)
    except NotRegularFileError:  # swapped since the check; no token, so the replacement is rolled back
        return ""
    if len(raw) > _MAX_TOKEN_BYTES or not raw.isascii():
        return ""
    return raw.decode("ascii")


def _recover(root: Path, plugin_id: str, version: str, recorded_install_id: str | None) -> None:
    """Finish or undo a replacement a crash interrupted.

    ``.done`` means the commit point was reached, so the new copy stands. An open
    ``.previous`` is decided by the install record: if it already carries the
    install id this replacement wrote to ``.txn`` (the caller saved its record,
    then died before the rename), the replacement committed; otherwise the old
    copy and its receipt go back, even when the new copy was already renamed in.
    """
    final = install_dir(root, plugin_id, version)
    previous = _previous(root, final)
    txn = _aside(root, final, "txn")
    if previous.is_dir() and not _aside(root, final, "done").exists():
        token = _txn_token(txn)
        if token and token == recorded_install_id:
            os.replace(previous, _aside(root, final, "done"))
        else:
            _rollback(root, final)
    _finish(root, final)


def recover_replacements(root: Path, recorded: Mapping[str, tuple[str, str]]) -> dict[str, str]:
    """Finish or undo every replacement a crash interrupted. Run at boot, before anything is read.

    ``recorded`` maps each recorded plugin id to its (version, install id).
    Returns the plugins whose replacement could not be finished or undone, with
    why: their files and receipt may not match their record, so they must not load.
    """
    staging = _contained(root, root / STAGING_DIRNAME)  # a linked staging folder would delete outside root
    failed: dict[str, str] = {}
    if staging.is_file():  # corruption: a file in the folder's place would fail every later install
        staging.unlink()
    if not staging.is_dir():
        return failed
    for path in sorted(staging.iterdir()):
        if _cut_short(path.name):
            shutil.rmtree(_contained(root, path), ignore_errors=True)
            continue
        name, _, suffix = path.name.rpartition(".")
        plugin_id, at, version = name.partition("@")
        if not at or not is_plugin_id(plugin_id) or not _is_version(version):
            continue  # not a marker this installer wrote: '..' as a version would name the plugins folder itself
        if suffix not in {"previous", "done", "txn"}:  # a lone .txn is what a finished rollback left
            continue
        record = recorded.get(plugin_id)
        try:
            _recover(root, plugin_id, version, record[1] if record and record[0] == version else None)
        except (InstallError, OSError) as exc:  # a locked receipt on Windows, a planted link: retried next boot
            failed[plugin_id] = f"an interrupted update of {version} could not be finished or undone: {exc}"
    return failed


def _cut_short(name: str) -> bool:
    """True for a staging folder or receipt copy a crash cut short; nothing refers to either.

    Boot recovery runs before any install, so a ``<plugin-id>-<uuid>`` staging
    folder is never one a running install is still writing.
    """
    stem, dash, token = name.rpartition("-")
    if dash and is_plugin_id(stem) and len(token) == 32 and all(c in "0123456789abcdef" for c in token):
        return True
    marker, _, suffix = name.rpartition(".")
    plugin_id, at, version = marker.partition("@")
    return bool(at) and is_plugin_id(plugin_id) and _is_version(version) and suffix.startswith("receipt-")


def _is_version(value: str) -> bool:
    try:
        semver_key(value)
    except ManifestError:
        return False
    return True


def _open_replacement(root: Path, final: Path) -> bool:
    return _previous(root, final).is_dir() and not _aside(root, final, "done").exists()


def _commit(root: Path, staging: Path, final: Path, then: Callable[[], None] | None, install_id: str | None) -> None:
    """Rename the staged copy into place, keeping the old copy until the install has landed.

    The old copy is renamed aside, never deleted first, and kept until ``then``
    (the caller's receipt and state writes) succeeds. Renaming it to ``.done`` is
    the commit point: before it, a failure here puts the old copy and its receipt
    back, and after a crash :func:`recover_replacements` decides from the install
    record whether ``then`` finished. ``install_id`` is the id ``then`` records.
    """
    if _open_replacement(root, final):
        raise InstallError(
            "recovery-pending",
            f"an earlier install of {final.parent.name} {final.name} was interrupted; restart the engine first",
        )
    _clear_leftovers(root, final)
    txn = _aside(root, final, "txn")
    if install_id:
        write_atomic(txn, install_id.encode("utf-8"))
    previous = _previous(root, final)
    if final.exists() and not final.is_dir():
        final.unlink()  # a stray file in a version's place is no copy to keep, and rollback restores folders only
    if final.exists():
        os.replace(final, previous)
    else:
        previous.mkdir()
    try:
        os.replace(staging, final)
        if then is not None:
            then()
    except BaseException:
        _rollback(root, final)
        txn.unlink(missing_ok=True)
        raise
    try:
        os.replace(previous, _aside(root, final, "done"))
    except OSError:
        if then is None or not install_id:
            raise
        # The records already name this install, so it has landed: the next boot
        # finds the open .previous, matches .txn to the record, and commits it.
        return
    _finish(root, final)


@contextmanager
def _opened_archive(archive: bytes) -> Iterator[tuple[bytes, PluginManifest, dict[str, Opener]]]:
    """The archive's manifest and an opener per listed member; members stay unread until opened."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as exc:
        raise InstallError("bad-archive", f"not a zip archive: {exc}") from exc
    with zf:
        manifest_bytes, payload = _read_zip(zf)
        try:
            manifest = parse_manifest(manifest_bytes)
        except ManifestError as exc:
            raise InstallError("bad-manifest", str(exc)) from exc
        _check_listing(manifest, payload)
        yield manifest_bytes, manifest, payload


def _read_zip(zf: zipfile.ZipFile) -> tuple[bytes, dict[str, Opener]]:
    manifest_bytes: bytes | None = None
    payload: dict[str, Opener] = {}
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise InstallError("bad-archive", f"symlink entry refused: {info.filename}")
        total += info.file_size
        if total > MAX_UNPACKED_BYTES:
            raise InstallError("bad-archive", "archive unpacks past the size limit")
        if info.filename == MANIFEST_FILENAME:
            manifest_bytes = _zip_manifest(zf, info)
            continue
        try:
            check_archive_path(info.filename)
        except ManifestError as exc:
            raise InstallError("bad-archive", str(exc)) from exc
        if info.filename in payload:
            raise InstallError("bad-archive", f"duplicate entry {info.filename}")
        payload[info.filename] = functools.partial(zf.open, info)
    if manifest_bytes is None:
        raise InstallError("bad-archive", f"archive has no {MANIFEST_FILENAME}")
    return manifest_bytes, payload


def install_archive(
    root: Path,
    archive: bytes,
    entry: CatalogEntry,
    *,
    then: Callable[[], None] | None = None,
    install_id: str | None = None,
) -> PluginManifest:
    """Install a store download. ``entry`` is the signed catalog row it came from.

    ``then`` runs once the new files are in place and before any old copy is
    deleted; if it raises, the old copy is put back. ``install_id`` is the id
    ``then`` writes into the install record, which lets crash recovery tell
    whether that record landed.
    """
    if len(archive) != entry.archive_size or sha256_hex(archive) != entry.archive_sha256:
        raise InstallError(
            "archive-mismatch",
            f"downloaded archive for {entry.manifest.plugin_id} does not match the signed catalog",
        )
    with _opened_archive(archive) as (manifest_bytes, manifest, payload):
        if manifest.to_wire() != entry.manifest.to_wire():
            raise InstallError("manifest-mismatch", "archive manifest differs from the signed catalog entry")
        _write_staged(root, manifest, manifest_bytes, payload, then, install_id)
    return manifest


def _zip_manifest(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    with zf.open(info) as handle:
        data = handle.read(MAX_INDEX_BYTES + 1)
    if len(data) > MAX_INDEX_BYTES:
        raise InstallError("bad-manifest", f"{MANIFEST_FILENAME} is larger than any signed index holds")
    return data


def check_archive(archive: bytes, *, max_bytes: int = MAX_ARCHIVE_BYTES) -> PluginManifest:
    """Every check an install makes on an archive's own bytes, without installing it.

    The publishing tool runs this before signing, so it never signs an archive
    every client would refuse, including one past the size every client's
    download stops at (``max_bytes``, the install route's own cap).
    """
    if len(archive) > max_bytes:
        raise InstallError("too-large", f"archive is {len(archive)} bytes; clients download at most {max_bytes}")
    with _opened_archive(archive) as (_, manifest, payload):
        for rel, opener in payload.items():
            _streamed(manifest, rel, opener)
    return manifest


def _source_manifest(source: Path, environ: Mapping[str, str] | None) -> tuple[bytes, PluginManifest]:
    if not dev_mode(environ):
        raise InstallError("dev-mode-off", f"sideloading needs {DEV_ENV}=1 in the engine environment")
    manifest_path = source / MANIFEST_FILENAME
    for path in (source, manifest_path):
        if path.is_symlink():  # every file would be read from the link's target
            raise InstallError("unsafe-path", f"{path} is a symlink; sideload a real folder of regular files")
    if not manifest_path.is_file():
        raise InstallError("bad-archive", f"{source} has no {MANIFEST_FILENAME}")
    try:
        payload = read_manifest_bytes(manifest_path)
        return payload, parse_manifest(payload)
    except NotRegularFileError as exc:
        raise InstallError("bad-archive", f"{source}: {exc}") from exc
    except ManifestError as exc:
        raise InstallError("bad-manifest", str(exc)) from exc


def preview_directory(source: Path, *, environ: Mapping[str, str] | None = None) -> tuple[PluginManifest, str]:
    """What a sideload of ``source`` would ask consent for, and the sha256 of that exact manifest.

    Reads the manifest only; runs nothing. A sideload passes the digest back so
    consent covers only the build that was shown.
    """
    manifest_bytes, manifest = _source_manifest(source, environ)
    return manifest, sha256_hex(manifest_bytes)


def _source_file(source: Path, rel: str) -> IO[bytes]:
    """A sideload file's bytes, refused when it or any folder on the way to it is a symlink.

    Checking only the file misses a linked parent folder, which would copy
    bytes from outside the source into the install.
    """
    path = source
    for part in Path(rel).parts:
        path = path / part
        if path.is_symlink():
            raise InstallError("unsafe-path", f"{rel}: {part} is a symlink; plugins ship regular files only")
    if not path.is_file():
        raise InstallError("missing-file", f"{rel} is missing or not a regular file")
    try:
        return open_regular_file(path)
    except NotRegularFileError as exc:  # swapped since the check above
        raise InstallError("missing-file", f"{rel} is missing or not a regular file") from exc


def install_directory(
    root: Path,
    source: Path,
    *,
    environ: Mapping[str, str] | None = None,
    consent: Callable[[PluginManifest, str], None] | None = None,
    then: Callable[[], None] | None = None,
    install_id: str | None = None,
) -> PluginManifest:
    """Developer sideload. Refused unless ``MDT_PLUGIN_DEV=1``.

    ``consent`` sees the parsed manifest and its sha256 before anything is written and
    raises :class:`InstallError` to refuse, so a sideload asks for what a store install
    asks for. ``then`` is as for :func:`install_archive`.
    """
    manifest_bytes, manifest = _source_manifest(source, environ)
    if consent is not None:
        consent(manifest, sha256_hex(manifest_bytes))
    # Checked again as each file is opened, since nothing is read until it is staged.
    payload: dict[str, Opener] = {rel: functools.partial(_source_file, source, rel) for rel in manifest.files}
    total = sum(_source_size(source, rel) for rel in manifest.files)
    if total > MAX_UNPACKED_BYTES:  # before staging: no need to copy 512 MiB to learn this
        raise InstallError("too-large", f"the build is {total} bytes; the unpacked limit is {MAX_UNPACKED_BYTES}")
    _write_staged(root, manifest, manifest_bytes, payload, then, install_id)
    return manifest


def _source_size(source: Path, rel: str) -> int:
    """A listed source file's size for the up-front limit check; one that cannot be read fails when staged."""
    try:
        return (source / rel).stat().st_size
    except OSError:
        return 0


def read_installed_manifest(root: Path, plugin_id: str, version: str) -> PluginManifest:
    path = _contained(root, install_dir(root, plugin_id, version)) / MANIFEST_FILENAME
    try:
        manifest = parse_manifest(read_manifest_bytes(path))
    except NotRegularFileError as exc:  # a pipe or device in its place was put there, never installed
        raise InstallError("tampered", f"{plugin_id} {version}: {exc}") from exc
    except OSError as exc:
        raise InstallError("not-installed", f"{plugin_id} {version}: {exc}") from exc
    except ManifestError as exc:
        raise InstallError("bad-manifest", f"{plugin_id} {version}: {exc}") from exc
    # The blocklist and every caller judged the record's identity; never load another plugin under it.
    if (manifest.plugin_id, manifest.version) != (plugin_id, version):
        raise InstallError(
            "bad-manifest",
            f"{plugin_id} {version}: its folder holds {manifest.plugin_id} {manifest.version}",
        )
    return manifest


def _sweep_unlisted(base: Path, manifest: PluginManifest) -> None:
    """Drop bytecode caches, then refuse any file the manifest does not list.

    Python reads ``__pycache__/*.pyc`` before the source it was built from, so
    a planted .pyc would run unsigned code past the source hashes. Caches are
    rebuilt from the verified sources, so removing them costs one recompile.
    """
    for cache in sorted(base.rglob("__pycache__"), reverse=True):
        if cache.is_dir() and not cache.is_symlink():
            shutil.rmtree(cache)
    listed = set(manifest.files) | {MANIFEST_FILENAME}
    for path in base.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            continue
        rel = path.relative_to(base).as_posix()
        if rel not in listed:
            raise InstallError("tampered", f"{rel} is in the installed copy but not in its manifest")


def verify_installed(root: Path, manifest: PluginManifest) -> None:
    """Re-hash every installed file and refuse extras. Raises InstallError('tampered', ...)."""
    # Before the sweep deletes caches: through a linked version folder it would delete outside root.
    base = _contained(root, install_dir(root, manifest.plugin_id, manifest.version))
    _sweep_unlisted(base, manifest)
    budget = MAX_UNPACKED_BYTES  # no install unpacks more, so a bigger payload changed after install
    for rel, digest in manifest.files.items():
        path = base / rel
        if path.is_symlink() or not path.is_file():
            raise InstallError("tampered", f"{rel} is missing from the installed copy")
        try:
            hashed = None if path.stat().st_size > budget else file_sha256(path, budget)
        except NotRegularFileError as exc:
            raise InstallError("tampered", f"{rel}: {exc}") from exc
        if hashed is None:
            raise InstallError("tampered", f"{rel} makes the installed copy larger than {MAX_UNPACKED_BYTES} bytes")
        if hashed[0] != digest:
            raise InstallError("tampered", f"{rel} changed after install")
        budget -= hashed[1]


def open_regular(path: Path) -> IO[bytes]:
    """``path`` opened for reading, refused as 'tampered' unless it is a regular file.

    Opened non-blocking and checked on the open descriptor, so a FIFO or
    device swapped in after install can neither hang the open or the first
    read nor be read at all.
    """
    try:
        return open_regular_file(path)
    except NotRegularFileError as exc:
        raise InstallError("tampered", str(exc)) from exc


def _binary(path: Path) -> IO[bytes]:
    return open_regular(path)


def snapshot_python(root: Path, manifest: PluginManifest, dest: Path) -> Path:
    """Copy the module's Python files into ``dest`` while hashing them; return the copy's ``python/apps``.

    The engine imports from this copy, so the code it runs is exactly the bytes
    hashed here, never a file swapped into the install tree after
    :func:`verify_installed` checked it. Raises InstallError('tampered', ...).
    """
    base = _contained(root, install_dir(root, manifest.plugin_id, manifest.version))
    left = MAX_UNPACKED_BYTES  # a file grown past this since the boot check is never copied whole
    for rel in manifest.files:
        if not rel.startswith("python/"):
            continue
        source = _contained(base, base / rel)
        if not source.is_file():
            raise InstallError("tampered", f"{rel} is missing from the installed copy")
        if source.stat().st_size > left:
            raise InstallError("tampered", f"{rel} changed after install: it passes the unpacked limit")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as sink:
                left -= _streamed(manifest, rel, functools.partial(_binary, source), sink, limit=left)
        except InstallError as exc:
            raise InstallError("tampered", f"{rel} changed after install ({exc})") from exc
    return dest / "python" / "apps"


def receipt_archive_sha256(root: Path, plugin_id: str, version: str) -> str | None:
    """The archive SHA-256 the installed build's receipt names, or None when there is none to read.

    Unverified: it only decides whether the store offers a same-version
    rebuild. Installing that build verifies everything again.
    """
    index, _ = receipt_paths(root, plugin_id, version)
    try:
        payload = read_capped(index, MAX_INDEX_BYTES)
        if payload is None:  # larger than any signed index: unreadable, so the store offers the repair build
            return None
        raw = json.loads(payload)
        for entry in raw["plugins"]:
            if entry["manifest"]["id"] == plugin_id:
                sha = entry["archive_sha256"]
                return sha if isinstance(sha, str) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def check_app_version(manifest: PluginManifest, app_version: str) -> None:
    """Refuse a plugin that declares a newer ``min_app_version`` than this build."""
    if manifest.min_app_version is None:
        return
    needed = semver_key(manifest.min_app_version)
    try:
        running = parse_semver(app_version)
    except ValueError:
        running = None
    if running is None or running < needed:
        raise InstallError(
            "needs-newer-app",
            f"{manifest.name} needs Open DJ {manifest.min_app_version} or newer; this is {app_version}",
        )


def write_receipt(root: Path, manifest: PluginManifest, payload: bytes, signature: bytes) -> None:
    """Keep the signed index a store install came from, outside the plugin's own files."""
    index, sig = receipt_paths(root, manifest.plugin_id, manifest.version)
    _receipt_folder(root, manifest.plugin_id)  # a stray file there would fail every install of this plugin
    index.parent.mkdir(parents=True, exist_ok=True)
    _contained(root, index)
    write_atomic(index, payload)
    write_atomic(sig, signature)


@contextmanager
def receipt_written(root: Path, manifest: PluginManifest, payload: bytes, signature: bytes) -> Iterator[None]:
    """Write the receipt, and put back whatever it replaced if the writes or the block raise.

    The old receipt is first copied aside in one rename, so an install's ``then``
    that crashes part way is undone by :func:`recover_replacements` too. The copy stays until
    the install commits.
    """
    final = install_dir(root, manifest.plugin_id, manifest.version)
    backup = _receipt_backup(root, final)
    paths = receipt_paths(root, manifest.plugin_id, manifest.version)
    _contained(root, paths[0].parent)
    if not backup.exists():  # a backup already there is the older, still-uncommitted state
        partial = _aside(root, final, f"receipt-{uuid.uuid4().hex}")
        partial.mkdir(parents=True)
        for path, limit in zip(paths, (MAX_INDEX_BYTES, _MAX_SIGNATURE_BYTES), strict=True):
            saved = read_capped(path, limit) if path.is_file() else None
            if saved is not None:  # one past its cap can never verify, so it is not kept to be put back
                (partial / path.name).write_bytes(saved)
        os.replace(partial, backup)
    try:
        write_receipt(root, manifest, payload, signature)
        yield
    except BaseException:
        _restore_receipt(root, final)
        raise


def verify_receipt(
    root: Path, manifest: PluginManifest, channel: str, *, trusted: Mapping[str, str] | None = None
) -> None:
    """The installed manifest must be the one a trusted key signed. Raises 'tampered'."""
    label = f"{manifest.plugin_id} {manifest.version}"
    index, sig = receipt_paths(root, manifest.plugin_id, manifest.version)
    try:
        payload, signature = read_capped(index, MAX_INDEX_BYTES), read_capped(sig, _MAX_SIGNATURE_BYTES)
    except OSError as exc:
        raise InstallError("tampered", f"{label} has no signed install receipt ({exc})") from exc
    if payload is None or signature is None:
        raise InstallError("tampered", f"{label}: install receipt is larger than any signed index")
    try:
        entry = parse_index(payload, signature, channel, trusted=trusted).find(manifest.plugin_id)
    except CatalogError as exc:
        raise InstallError("tampered", f"{label}: install receipt does not verify ({exc})") from exc
    if entry is None or entry.manifest.to_wire() != manifest.to_wire():
        raise InstallError("tampered", f"{label}: installed manifest is not the one the store signed")


def sweep_unrecorded(root: Path, recorded: Mapping[str, str]) -> None:
    """Delete installed versions and receipts that no install record names.

    Run at boot, before anything loads: a version kept because it was running
    when it was updated or uninstalled is no longer in use, so it goes now.
    ``recorded`` maps each recorded plugin id to its recorded version.
    """
    ids = {
        child.name
        for base in (root, root / RECEIPTS_DIRNAME)
        if base.is_dir()
        for child in base.iterdir()
        if is_plugin_id(child.name)
    }  # a stray file too: it blocks a later install
    for plugin_id in sorted(ids):
        remove_installed(root, plugin_id, keep=[recorded[plugin_id]] if plugin_id in recorded else [])


def _folder_or_unlink(root: Path, path: Path) -> Path:
    """``path`` checked to stay under ``root``; a plain file in a plugin folder's place is unlinked.

    Left behind, it would make every later install of that plugin fail to
    create its folder. A link there is still refused, as everywhere else.
    """
    path = _contained(root, path)
    if path.is_file() and path != root / STATE_FILENAME:  # 'state.json' is a valid plugin id too
        path.unlink()
    return path


def _receipt_folder(root: Path, plugin_id: str) -> Path:
    """A plugin's receipt folder, with a plain file in its or the receipts root's place unlinked."""
    _folder_or_unlink(root, root / RECEIPTS_DIRNAME)
    return _folder_or_unlink(root, root / RECEIPTS_DIRNAME / plugin_id)


def _remove_path(path: Path) -> None:
    """Remove a real folder with its contents, or a file or link (never what a link points at)."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


#: Every call :func:`_clear_folder` makes through a directory descriptor is available here.
_CLEAR_BY_DESCRIPTOR = (
    shutil.rmtree.avoids_symlink_attacks
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and {os.open, os.stat, os.unlink, os.rmdir} <= os.supports_dir_fd
    and os.scandir in os.supports_fd
)


def _clear_folder(folder: Path, keep: Callable[[str], bool]) -> None:
    """Remove every entry of ``folder`` that ``keep`` does not claim, then the folder once it is empty.

    Platform seam. Where the OS can (macOS, Linux), the folder is opened once
    without following a link and cleared through that descriptor, so a folder
    swapped for a link after its checks is refused, never walked: what is
    cleared is the folder that was validated. Elsewhere (Windows, where making a
    link needs a privilege) the entries are listed and removed by path. A plain
    file in the folder's place is left alone: there is nothing in it to clear.
    """
    if not _CLEAR_BY_DESCRIPTOR:
        _clear_by_path(folder, keep)
        return
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError as exc:  # a link in its place since the check: ELOOP, or ENOTDIR on Linux
        if isinstance(exc, NotADirectoryError) and not folder.is_symlink():
            return  # a plain file
        raise InstallError("unsafe-path", f"{folder.name} is not a real folder ({exc})") from exc
    try:
        empty = _clear_descriptor(fd, keep)
    finally:
        os.close(fd)
    if empty:
        os.rmdir(folder)  # refuses a link swapped in since, so nothing outside is touched


def _clear_descriptor(fd: int, keep: Callable[[str], bool]) -> bool:
    """Clear the open folder ``fd`` of what ``keep`` does not claim; True when it is left empty."""
    with os.scandir(fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        if keep(name):
            continue
        if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
            shutil.rmtree(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)  # a file or link: never what a link points at
    with os.scandir(fd) as entries:
        return next(entries, None) is None


def _clear_by_path(folder: Path, keep: Callable[[str], bool]) -> None:
    if not folder.is_dir() or folder.is_symlink():
        return
    for child in folder.iterdir():
        if not keep(child.name):
            _remove_path(child)
    if not any(folder.iterdir()):
        folder.rmdir()


def remove_installed(root: Path, plugin_id: str, keep: Iterable[str] = ()) -> None:
    """Delete every installed version of a plugin, and its receipt, except ``keep``."""
    kept = set(keep)
    _clear_folder(_folder_or_unlink(root, root / plugin_id), lambda name: name in kept)
    # A folder in a receipt's place is removed too: it would stop every retry here.
    _clear_folder(
        _folder_or_unlink(root, root / RECEIPTS_DIRNAME / plugin_id),
        lambda name: name.removesuffix(".sig").removesuffix(".index.json") in kept,
    )
