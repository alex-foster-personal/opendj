"""The cross-process lock every writer of one stem bundle directory holds.

One stable_id's bundle directory (``<data>/state/<stem root>/<stable_id>``) is
written by several processes: a stems job's publish
(``apps.stems.bundle_publish.publish_stems_local`` for the relay and direct
Modal routes, ``scripts/stem_bundle_worker.py`` for the local route), an R2
hydrate (``apps.cloud.stem_hydration.hydrate_one``), the lyrics RoFormer
writers (``apps.lyrics.register_stems``, ``apps.lyrics.stems_sync``, through
:func:`store_writer_lock`), the GCE farm import (``apps.stems.farm_import``), and the STEM-58
re-compress pass (``apps.stems.recompress``). Each one holds THIS lock while it
creates, replaces or moves that directory, so no two of them interleave on it:
a job publishing mid-recompress cannot be clobbered by the recompress swap, nor
a half-written bundle moved into its backup.

It is the lock hydration already held (``stem_hydration.hydrate_lock_path``),
named here so the stems writers share it without importing
the hydration module: the same 256 striped files under
``<data>/state/stem-hydrate-locks/``, so the lock files never grow with the
library and two ids sharing a stripe only wait on each other. The OS lock is
``apps.cloud.stem_upload_queue.exclusive_file_lock`` (``fcntl.flock`` on POSIX,
``msvcrt.locking`` on Windows); the OS drops it when the holder dies, so a
crashed writer never leaves a stale lock.

Hold it for the publish or swap itself, never for a separation or an encode:
a deck load's hydrate waits on it.

-Claude
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

from apps.cloud.stem_upload_queue import exclusive_file_lock

log = logging.getLogger(__name__)

#: Stripes of the cross-process bundle lock (was ``HYDRATE_LOCK_STRIPES``).
BUNDLE_LOCK_STRIPES: int = 256
LOCK_DIR_NAME: str = "stem-hydrate-locks"


def bundle_lock_path(data_dir: Path, stable_id: str) -> Path:
    """The OS lock file every writer of ``stable_id``'s bundle holds."""
    stripe = int(hashlib.sha256(stable_id.encode("utf-8")).hexdigest(), 16) % BUNDLE_LOCK_STRIPES
    return Path(data_dir) / "state" / LOCK_DIR_NAME / f"{stripe:03d}.lock"


def data_dir_of_bundle(bundle_dir: Path) -> Path:
    """``<data>`` for ``<data>/state/<stem root>/<stable_id>``; refuses any other shape.

    A bundle outside a ``state`` directory has no shared lock to hold, and
    guessing one would let two writers lock different files and both run.
    """
    bundle_dir = Path(bundle_dir)
    state = bundle_dir.parent.parent
    if state.name != "state":
        raise ValueError(
            f"{bundle_dir} is not <data>/state/<stem root>/<stable_id>, so no shared bundle lock exists for it"
        )
    return state.parent


@contextlib.contextmanager
def bundle_lock(bundle_dir: Path) -> Iterator[None]:
    """Hold the cross-process lock for ``bundle_dir`` (blocking)."""
    bundle_dir = Path(bundle_dir)
    with exclusive_file_lock(bundle_lock_path(data_dir_of_bundle(bundle_dir), bundle_dir.name)):
        yield


@contextlib.contextmanager
def held_for_read(data_dir: Path, stable_id: str) -> Iterator[None]:
    """Hold ``stable_id``'s bundle lock while a reader loads a bundle and takes its identity.

    For a reader that opens the parts one after another (vocal regions from
    stems): every swap and eviction of a shared-store bundle holds this lock
    (:func:`bundle_lock`), so an identity taken under it names one whole
    generation. The decode itself runs after it is released, and the reader
    takes the identity again under it to prove nothing was swapped in
    between (``apps.vocals.from_stems.derive_from_generation``): a decode
    held here would make every writer of an id on the same stripe wait it
    out.
    Keyed on ``data_dir`` and the id, not a path, so a stem root symlinked to
    another volume still finds the lock its writers hold. Hold it with no
    other stems lock: a swap takes this one first.
    """
    with exclusive_file_lock(bundle_lock_path(data_dir, stable_id)):
        yield


def is_shared_store_bundle(bundle_dir: Path) -> bool:
    """True when ``bundle_dir`` sits at ``<data>/state/<stem root>/<stable_id>``."""
    return Path(bundle_dir).parent.parent.name == "state"


@contextlib.contextmanager
def store_writer_lock(bundle_dir: Path) -> Iterator[None]:
    """The bundle lock for a writer whose root may lie outside a data dir.

    The lyrics RoFormer writers (``apps.lyrics.register_stems.register_pair``
    and ``apps.lyrics.stems_sync.hydrate``) write ``<data>/state/
    stems-roformer-spike`` in production, which the STEM-58 pass and eviction
    both rewrite, so there they hold :func:`bundle_lock` like every other
    writer. Pointed at a root outside any
    ``<data>/state`` (a tool's scratch root, a test's tmp root), no
    re-compress or eviction pass enumerates it, so no lock exists to share
    and none is taken.
    """
    if not is_shared_store_bundle(bundle_dir):
        yield
        return
    with bundle_lock(bundle_dir):
        yield


#: Where :func:`replace_bundle_dir` keeps the old bundle mid-swap:
#: ``<beside the real stem root>/.stem-bundle-swap/<real stem root's name>/<stable_id>``,
#: outside every stem root so no scan or eviction pass mistakes it for a bundle.
SWAP_DIR_NAME: str = ".stem-bundle-swap"


def beside_stem_root(stem_root: Path) -> Path:
    """The directory that holds ``stem_root``'s real directory, on its filesystem.

    A stem root can be a symlink to another volume (``(private path)`` on
    an external disk). A staging or backup directory placed in the lexical
    parent would then sit on the data dir's volume, and the rename that
    publishes or sets aside a bundle would fail with EXDEV. Every directory a
    bundle is renamed into or out of is therefore placed beside the RESOLVED
    stem root. The lock stays keyed on the lexical path
    (:func:`data_dir_of_bundle`), which every writer of the store shares.
    """
    return Path(os.path.realpath(stem_root)).parent


def swap_backup_path(dest: Path) -> Path:
    """The one place ``dest``'s old bundle waits while a swap is under way.

    Keyed on the destination alone, not on the writer or its pid, so the next
    writer of the same bundle finds what a writer that died mid-swap left.
    The namespace is the RESOLVED stem root's name: two data dirs whose
    ``state/stems`` symlink to sibling directories on one volume share the
    directory beside them but never a real root's name, so neither can
    discard or restore the other's backup (Codex on #6820).
    """
    dest = Path(dest)
    if not is_shared_store_bundle(dest):
        # A bundle outside <data>/state (a worker's --out-dir such as
        # /tmp/bundle) keeps its backup beside it, in the directory the caller
        # can already write, never in its grandparent.
        return dest.parent / f".{dest.name}{SWAP_DIR_NAME}"
    real_root = Path(os.path.realpath(dest.parent))
    return real_root.parent / SWAP_DIR_NAME / real_root.name / dest.name


_TRASH_INFIX = ".trash-"


def _discard_backup(backup: Path) -> None:
    """Remove a swap's old bundle without ever failing the swap that replaced it.

    On Windows a part a deck is still streaming was opened with
    ``FILE_SHARE_DELETE`` (``apps.shared.shared_read_open``), so it can be
    renamed and deleted, but on a volume without POSIX delete semantics its
    name stays until the last handle closes and ``rmtree`` then fails on the
    directory holding it. The backup is first renamed to a
    unique trash name, which frees the backup slot for the next swap at
    once, and then removed; what cannot be removed yet stays as trash, is
    logged, and is swept by a later swap of the same bundle.
    """
    trash = backup.with_name(f"{backup.name}{_TRASH_INFIX}{uuid.uuid4().hex}")
    try:
        backup.rename(trash)
    except OSError:
        trash = backup
    try:
        shutil.rmtree(trash)
    except OSError as exc:
        log.warning("stem bundle backup %s kept for a later sweep: %s", trash, exc)


def _sweep_trash(backup: Path) -> None:
    """Remove the trash earlier swaps of this bundle could not remove yet.

    Trash that still cannot be removed (a deck on Windows still streaming one
    of its parts) is logged by path and kept for the next sweep, as
    :func:`_discard_backup` does, rather than dropped silently (Sol on
    #6820). It never fails the swap: trash holds no live bundle, and raising
    here would refuse every publish of this bundle while that deck plays.
    """
    if not backup.parent.is_dir():
        return
    for stale in backup.parent.glob(f"{backup.name}{_TRASH_INFIX}*"):
        try:
            shutil.rmtree(stale)
        except OSError as exc:
            log.warning("stem bundle trash %s kept for a later sweep: %s", stale, exc)


@contextlib.contextmanager
def readers_held_off(bundle_dir: Path) -> Iterator[None]:
    """Hold the eviction-claim lock a deck's read waits on, for a swap's renames.

    A deck read holds that lock while it resolves and opens a bundle
    (``apps.cloud.stem_cache_budget.read_barrier``), so holding it across
    the two renames of a swap makes a manifest, part or waveform request
    that lands in the gap wait the milliseconds the swap takes instead of
    finding no bundle, or falling through to a lower-precedence root. This
    is the one helper a writer takes it through. Taken inside
    :func:`bundle_lock`, the order the
    eviction claim takes them in. A bundle outside ``<data>/state`` (a
    worker's ``--out-dir``) has no reader to hold off, and none is taken.
    """
    bundle_dir = Path(bundle_dir)
    if not is_shared_store_bundle(bundle_dir):
        yield
        return
    # Function-local: stem_cache_budget imports this module.
    from apps.cloud.stem_cache_budget import eviction_claim_lock_path

    with exclusive_file_lock(eviction_claim_lock_path(data_dir_of_bundle(bundle_dir), bundle_dir.name)):
        yield


def fsync_dir(directory: Path) -> None:
    """Flush ``directory``'s entries to disk. Platform seam: Windows cannot
    open a directory for fsync, and NTFS journals its metadata itself."""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_durably(source: Path, destination: Path) -> None:
    """Rename ``source`` to ``destination`` and flush both parents, so the move
    is on disk before anything relies on it: a rollback or repair that a
    power loss could undo would leave the bundle path empty."""
    source.rename(destination)
    fsync_dir(destination.parent)
    if source.parent != destination.parent:
        fsync_dir(source.parent)


def undo_failed_swap(dest: Path, backup: Path, new_dir: Path, *, replacing: bool = True) -> None:
    """Put ``dest`` back as it was before a swap that raised, whichever rename landed.

    A swap is two renames, ``dest`` to ``backup`` and ``new_dir`` to
    ``dest``, each followed by directory flushes, and a flush can raise
    after its rename landed. So what moved is read off the disk: a new
    bundle that landed at ``dest`` goes back to ``new_dir`` for its owner to
    clean up, then the old one comes back from ``backup``. With
    ``replacing`` false there was no old bundle, and ``dest`` is left empty.
    Every rename runs before any flush, so a flush that keeps failing cannot
    leave ``dest`` empty with the old bundle still parked at ``backup``; the
    first flush error is raised once all of them were tried.
    """
    moves: list[tuple[Path, Path]] = []
    if not replacing:
        if dest.exists():
            moves.append((dest, new_dir))
    elif backup.exists():
        if dest.exists():
            moves.append((dest, new_dir))
        moves.append((backup, dest))
    for source, destination in moves:
        source.rename(destination)
    _flush_parents({parent for move in moves for parent in (move[0].parent, move[1].parent)})


def _flush_parents(parents: set[Path]) -> None:
    """Flush every directory in ``parents``, then raise the first error any flush raised."""
    failure: OSError | None = None
    for parent in sorted(parents):
        try:
            fsync_dir(parent)
        except OSError as error:
            failure = failure or error
    if failure is not None:
        raise failure


def _makedirs_durably(directory: Path) -> None:
    """Create ``directory`` and any missing ancestors, flushing each new entry to disk."""
    missing: list[Path] = []
    current = directory
    while not current.is_dir():
        missing.append(current)
        current = current.parent
    for created in reversed(missing):
        created.mkdir(exist_ok=True)
        fsync_dir(created.parent)


def file_flush_mode(platform: str = os.name) -> str:
    """The mode :func:`fsync_tree` opens a file in to flush it. Platform seam:
    Windows flushes through ``FlushFileBuffers``, which needs a handle with
    write access (Codex on #6820); ``r+b`` neither truncates nor creates. POSIX
    keeps ``rb`` so a read-only file still flushes."""
    return "r+b" if platform == "nt" else "rb"


def fsync_tree(root: Path) -> None:
    """Flush every regular file under ``root``, then every directory, deepest first."""
    directories = [root]
    mode = file_flush_mode()
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            directories.append(path)
        elif path.is_file():
            with path.open(mode) as handle:
                os.fsync(handle.fileno())
    for directory in sorted(directories, key=lambda d: len(d.parts), reverse=True):
        fsync_dir(directory)


def restore_orphaned_swap(dest: Path, data_dir: Path) -> bool:
    """Put back the bundle a writer killed between a swap's two renames left aside.

    Called by a deck read while it holds the bundle lock and then
    :func:`readers_held_off`, in a swap's own order. Every live swap of a
    shared-store bundle holds both across its renames, so under them a
    backup with no bundle at ``dest`` can only be what a dead writer left, and it is the only good copy. Without this
    the track read as missing until another publish of the same bundle. A
    bundle at ``dest`` means that swap landed and only
    its cleanup was lost; the next writer discards that backup. Returns
    whether a bundle was put back.
    """
    dest = Path(dest)
    # The bundle being there is the common case: one stat, nothing resolved.
    if dest.exists():
        return False
    if not is_shared_store_bundle(dest) or data_dir_of_bundle(dest).resolve() != Path(data_dir).resolve():
        return False
    backup = swap_backup_path(dest)
    if not backup.is_dir():
        return False
    _rename_durably(backup, dest)
    log.warning("stem bundle %s restored from the backup an interrupted swap left", dest)
    return True


def discard_landed_swap_backup(dest: Path) -> bool:
    """Delete the backup a landed swap of ``dest`` never cleaned up, while ``dest`` is still there.

    A writer killed after its new bundle reached ``dest`` and before
    :func:`_discard_backup` leaves both behind. That backup is an older
    render, and once something else removes ``dest`` (eviction) a deck read
    would take it for an interrupted swap and :func:`restore_orphaned_swap`
    would put the obsolete stems back. Eviction calls this
    under the bundle lock before it removes the bundle, so the backup goes
    while ``dest`` still proves the swap landed.

    Returns whether the swap slot is clear afterwards. False means a backup
    is still at :func:`swap_backup_path` (it could be neither moved to trash
    nor removed, as on Windows while a deck streams one of its parts), and
    the caller must keep ``dest``.
    """
    dest = Path(dest)
    if not dest.is_dir():
        return True
    backup = swap_backup_path(dest)
    if not backup.is_dir():
        return True
    _discard_backup(backup)
    fsync_dir(backup.parent)
    return not backup.exists()


def replace_bundle_dir(new_dir: Path, dest: Path) -> None:
    """Rename ``new_dir`` to ``dest``, keeping the old bundle until that rename lands.

    Invariants: the old bundle stays at ``dest``, or safely in
    :func:`swap_backup_path`, until the new one is in place and on disk; a
    swap that raises leaves ``dest`` as it was (:func:`undo_failed_swap`);
    readers are held off across both renames (:func:`readers_held_off`); a
    backup left by a writer killed mid-swap is repaired first. Callers hold
    :func:`bundle_lock` for ``dest``.
    """
    new_dir, dest = Path(new_dir), Path(dest)
    backup = swap_backup_path(dest)
    _sweep_trash(backup)
    with readers_held_off(dest):
        if backup.exists():
            if dest.exists():
                # A backup that stays (Windows, a deck streaming one of its
                # parts) would make the move aside below fail and its rollback
                # fail too, so this publish stops here with dest untouched
                # (Codex on #6820).
                if not discard_landed_swap_backup(dest):
                    raise SwapBackupStuckError(backup)
            else:
                _rename_durably(backup, dest)
    # The new parts reach the disk before any rename can expose them, and
    # both renames do before the old bundle is deleted: a power loss after
    # that delete must not leave a bundle whose parts are empty and no
    # backup. Flushed outside readers_held_off, so no read
    # waits on the file flushes, and after the repair above, which a missing
    # or unreadable new tree must not skip.
    fsync_tree(new_dir)
    with readers_held_off(dest):
        had_old = dest.exists()
        try:
            if had_old:
                # Only a bundle being replaced needs the backup directory: a
                # first publish to a shallow --out-dir (``/tmp/bundle``) must
                # not try to create ``/.stem-bundle-swap``.
                _makedirs_durably(backup.parent)
                dest.rename(backup)
                # The move aside reaches the disk before the gap it opens can
                # be exposed: a power loss before the second rename must leave
                # the backup recovery looks for. A flush that
                # raises after the move is rolled back like the rename below.
                fsync_dir(backup.parent)
                fsync_dir(dest.parent)
            new_dir.rename(dest)
            # The swap is reported done only once the move in is on disk; a
            # flush that raises here is rolled back like a failed rename.
            fsync_dir(dest.parent)
        except BaseException:
            undo_failed_swap(dest, backup, new_dir, replacing=had_old)
            raise
    if had_old:
        _discard_backup(backup)


class SwapBackupStuckError(OSError):
    """An earlier swap's backup could not be cleared, so a publish did not start; names the backup."""

    def __init__(self, backup: Path) -> None:
        super().__init__(f"stem bundle backup {backup} could not be cleared; the bundle beside it was left as it is")
        self.backup = backup


class StagingCleanupError(OSError):
    """A writer's staging directory could not be removed; names the directory."""


def remove_staging(staging: Path, what: str = "publish staging directory") -> None:
    """Remove a writer's staging directory, or raise :class:`StagingCleanupError` naming it.

    A swap that landed renamed it away, so it is usually gone. A failed write,
    carry-over copy or swap leaves a whole bundle there, which nothing else
    sweeps, so a removal that fails is reported by path rather than left on
    disk unseen. Raised from a writer's ``finally``, it keeps the writer's
    own error as its context. Every writer of a stem bundle that stages one
    (a publish, ``stems hydrate``, the farm import, the RoFormer register)
    cleans up through here (Sol on #6820).
    """
    if not staging.exists():
        return
    try:
        shutil.rmtree(staging)
    except OSError as exc:
        raise StagingCleanupError(f"could not remove {what} {staging}: {exc}") from exc


#: Where :func:`publish_bundle` writes a bundle before swapping it in:
#: ``.stem-publish/`` beside the resolved stem root, outside every stem root.
PUBLISH_DIR_NAME: str = ".stem-publish"


def _restore_orphaned_backup(bundle: Path) -> None:
    """Under ``bundle``'s lock: put back a backup a writer killed mid-swap left, before it is read.

    :func:`replace_bundle_dir` repairs the same backup, but only after
    :func:`publish_bundle` has chosen what to carry over, so the dead swap's
    non-part files were read as absent and then deleted with the backup
    (Codex on #6820).
    """
    backup = swap_backup_path(bundle)
    if bundle.exists() or not backup.is_dir():
        return
    with readers_held_off(bundle):
        if not bundle.exists() and backup.is_dir():
            _rename_durably(backup, bundle)
            log.warning("stem bundle %s restored from the backup an interrupted swap left", bundle)


def publish_bundle(bundle: Path, write: Callable[[Path], None], replaced: Callable[[Path], bool]) -> None:
    """Write a whole new bundle aside and swap it in for ``bundle``, taking its lock.

    ``write`` fills an empty staging directory on the stem root's own
    volume, and the staged tree is flushed, before :func:`bundle_lock` is
    taken: a hydrate or publish of an id sharing the stripe must not wait out
    a slow disk's writes. Under the lock, every top-level
    entry of the existing bundle for which ``replaced`` is false (a file that
    is not a stem part or the manifest) is copied in, so it survives the
    publish, and the staging directory replaces ``bundle`` through
    :func:`replace_bundle_dir`. Reading the old bundle only under the lock
    keeps a carried-over file from a generation another writer has since
    replaced, and a backup a writer killed mid-swap left is put back first,
    so its files are carried over too. Writing the parts into the live
    directory would truncate each one while a deck may be streaming it, and
    the web routes do not take the
    bundle lock. Callers must NOT hold the lock: it is
    not reentrant.
    """
    bundle = Path(bundle)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    staging_root = beside_stem_root(bundle.parent) / PUBLISH_DIR_NAME
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{bundle.name}-", dir=staging_root))
    try:
        write(staging)
        fsync_tree(staging)
        with bundle_lock(bundle):
            _restore_orphaned_backup(bundle)
            if bundle.is_dir():
                for entry in bundle.iterdir():
                    if replaced(entry) or (staging / entry.name).exists():
                        continue
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.copytree(entry, staging / entry.name, symlinks=True)
                    else:
                        shutil.copy2(entry, staging / entry.name, follow_symlinks=False)
            replace_bundle_dir(staging, bundle)
    finally:
        remove_staging(staging)


__all__ = [
    "BUNDLE_LOCK_STRIPES",
    "PUBLISH_DIR_NAME",
    "SWAP_DIR_NAME",
    "StagingCleanupError",
    "SwapBackupStuckError",
    "beside_stem_root",
    "bundle_lock",
    "bundle_lock_path",
    "data_dir_of_bundle",
    "discard_landed_swap_backup",
    "file_flush_mode",
    "fsync_dir",
    "fsync_tree",
    "held_for_read",
    "is_shared_store_bundle",
    "publish_bundle",
    "readers_held_off",
    "remove_staging",
    "replace_bundle_dir",
    "restore_orphaned_swap",
    "store_writer_lock",
    "swap_backup_path",
    "undo_failed_swap",
]
