"""Where the local music-backup manifest lives, and the one-shot move to it (#5639).

The manifest maps each mirrored track to its asset-store path and is the route
back for a restore. Its local name used to carry the house store alias
(``bifrost2-music-manifest.json``); since the store host became config
(MDT_B2STORE_SSH_HOST, issue #5639) the file is named for its role instead.
The remote copy was always ``<store root>/manifest.json`` and does not move.

Migration: ``migrate_music_manifest`` gives a legacy file the current name the
first time either reader or writer runs, so a checkout keeps its manifest
without a manual step.

The invariant is inode identity, and this module meets it in three ways.

First, the migration removes NOTHING. It hardlinks the legacy file to the
current name - ``os.link`` creates the destination exclusively, so a current
manifest that appears from another writer is never replaced - and then leaves
the legacy name in place. Both names are one inode, so the legacy file costs no
bytes, and no name can be deleted out from under a writer that rebound it while
the migration was running. That last point is the whole reason for the shape: a
"stat the name, then unlink it" removal is two syscalls and ``unlink`` takes a
name rather than a handle, so a writer that atomically replaces the legacy
manifest between the check and the removal loses its file. There is no portable
way to unlink conditionally on inode identity, so the removal does not happen
here at all; stage 2 of #5639 deletes the legacy name and this function
together.

Second, every identity this module DECIDES on is read with ``fstat`` from an
open handle, never with a fresh ``stat`` of a name. The success claim - "both
names are one file" - is returned only when one open handle per name pinned the
same ``(st_dev, st_ino)`` AND that identity is the one pinned before the link,
with every handle still open for the comparison. The pre-link descriptor is
held open until the call returns, so the inode it pinned cannot be unlinked,
freed and recycled onto a replacement that would then compare equal. A name
re-bound between two independent stats, or between the pre-link pin and the
link itself, therefore cannot produce a false success; and the
``FileExistsError`` arm of the link compares that same pinned pair, so it
cannot report "already migrated" for two different files either. Anything else
is a refusal naming both absolute paths.

Third, every name is inspected as a DIRECTORY ENTRY before its identity is
used. ``_open_pinned`` refuses a name whose entry is not a regular file: an
``lstat`` of the name, ``O_NOFOLLOW`` on the ``open`` where the platform has it,
an ``O_NONBLOCK`` (where the platform has it) so a FIFO swapped in after that
``lstat`` is opened rather than waited on forever, an ``fstat`` type check and
an identity comparison on the handle that was actually pinned, and a SECOND
``lstat`` of the name after the open that must still be that same regular
entry. The second inspection is what closes the window on a platform without
``O_NOFOLLOW``: the handle must be the entry that was inspected, and a symlink
swapped in during the window fails that even when it points at the expected
file and so carries the expected inode. A symlink
aliases ANOTHER name's inode and contributes no directory entry of its own, so
following it would let the current name compare equal to the legacy file while
no hardlink between the two names was ever created - and the documented cleanup
step, which removes the legacy name, would then leave the current name dangling
and the restore reader with a ``FileNotFoundError``. Target-identity equality is
therefore not accepted as proof of a completed migration; the entry has to be a
regular file in its own right, and it has to be the entry that was inspected.

What is NOT refused is a checkout with only the current name: no migration is
attempted there, so nothing is claimed about the entry, and a reader through a
symlink at that name works as it always did. The entry check applies to the
names this migration would otherwise prove were one file.

Guaranteed, each case pinned by a test in
``tests/scripts/test_backup_manifest_rename.py``:

- legacy only: the current name is created as a hardlink to it - same inode,
  same bytes - and a rerun is a no-op;
- both names are one file and that file is the one this migration opened:
  no-op, nothing written;
- both names are different files, including a current name that lands between
  the probe and the link: refusal, both files untouched;
- either name is a symlink to the other, or to anything else, or is any other
  non-regular directory entry: refusal naming both paths - the alias is not a
  hardlink and cannot survive the documented cleanup of the legacy name;
- the legacy name is re-bound by another writer while the migration runs,
  before or after the link: refusal, nothing deleted, the writer's file intact
  under the legacy name;
- the legacy name is removed while the migration runs: the link is the only
  copy left, so it stands and the current name keeps the bytes that were
  opened;
- the current name is removed or re-bound after the link: refusal, because the
  two names can no longer be confirmed as one file;
- a filesystem without hardlinks: refused explicitly rather than falling back
  to a replacing rename - a replacing rename is exactly the clobber this
  guards;
- no legacy name, or current only: untouched.

DECLARED PRECONDITION, not a guarantee: a writer that re-binds a NAME after
the handle on THAT name is open cannot be detected by any sequence of stdlib
POSIX calls - identity belongs to an open file, and a name can always be
re-bound afterwards. The two names are opened one after the other (the legacy
name first, at the pre-link pin; the current name at verification), so the
window for each name starts when its own handle opens, not when both are open.
Running the migration with every writer stopped is therefore the operator's
precondition, stated the same way for the manual ``rm`` recipe in
(private path), which is name-based in exactly the
same sense.

The writers of this manifest are this repo's own backup scripts
(``_write_manifest`` writes the current name, and both scripts migrate first),
so there is no separate writer protocol to coordinate with; a second instance
of them, or a pre-#5639 checkout, is the concurrent writer.

See (private path).

Stdlib only: imported by the PEP 723 backup scripts.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

#: Repo-relative path of the local manifest.
MUSIC_MANIFEST_REL: Path = Path("(private path)")
#: Pre-#5639 name. Read only by ``migrate_music_manifest``; stage 2 deletes it.
LEGACY_MUSIC_MANIFEST_REL: Path = Path("(private path)")

#: What ``(st_dev, st_ino)`` a directory entry is bound to.
Identity = tuple[int, int]


def _identity(stat: os.stat_result) -> Identity:
    """The inode identity of ``stat``: two names for one file share it."""
    return (stat.st_dev, stat.st_ino)


class _NonRegularEntry(Exception):
    """A name this module decides on is not a regular directory entry.

    Raised by ``_open_pinned`` and turned into the module's ``FileExistsError``
    refusal by ``_pinned_or_refuse``, which is the only place that knows both
    absolute paths every refusal has to name.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(str(path))
        self.path = path


class _RacedEntry(Exception):
    """A name was re-bound between its entry check and its open.

    Raised by ``_open_pinned`` when what it pinned, or what the name holds after
    the open, is not the directory entry the preceding ``lstat`` inspected. On a
    platform without ``O_NOFOLLOW`` a symlink swapped into that window lands
    here: the handle carries the link's TARGET, and the name itself is a symlink
    again by the time it is re-inspected. ``_pinned_or_refuse`` turns it into the
    module's ``FileExistsError`` refusal, which is the only place that knows both
    absolute paths.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(str(path))
        self.path = path


def _open_pinned(path: Path, open_fds: list[int]) -> Identity | None:
    """Open ``path`` and return the identity its handle pins, or ``None``.

    The identity comes from ``fstat`` on the open handle, never from a ``stat``
    of the name, so it describes the file the caller is holding rather than the
    name, which another writer can re-bind at any moment. The descriptor is
    appended to ``open_fds`` and left open: the identity stays pinned to that
    file until the caller closes it.

    The DIRECTORY ENTRY is checked before the identity is trusted, and again on
    the handle that was actually opened and on the name it was opened by.
    ``os.open`` follows a symlink, which would hand back the ``fstat`` of the
    link's TARGET: a symlinked current name would then carry the legacy file's
    identity, equal on every comparison, although no hardlink between the two
    names exists. So a symlink - and any other entry whose handle is not a
    regular file - raises ``_NonRegularEntry`` instead of being followed, and a
    name that is not the entry the ``lstat`` inspected raises ``_RacedEntry``.
    ``O_NOFOLLOW`` is the cheap refusal where the platform defines it (``ELOOP``
    means the name became a symlink in between). Where it is absent the entry is
    re-inspected AFTER the open: identity equality alone cannot see a link that
    points at the expected file, because the link's target carries that same
    identity, but the name itself is then a symlink rather than a regular file.

    ``O_NONBLOCK`` is what keeps that window from becoming a hang where the
    platform defines it: a name swapped for a FIFO between the ``lstat`` and the
    open would otherwise leave a bare ``O_RDONLY`` waiting for a writer that
    never comes, so neither ``fstat`` nor the refusal it leads to would run.
    """
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(entry.st_mode):
        raise _NonRegularEntry(path)
    inspected = _identity(entry)
    # Each flag only where the platform defines it; the lstat above, the fstat
    # below and the identity comparison between them are what hold everywhere.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        # Either the name is gone - nothing to pin - or it became a DANGLING
        # symlink inside the window, which an open without O_NOFOLLOW reports
        # the same way. Only a second look at the name tells them apart, and a
        # symlink is the entry refusal, never "no manifest here".
        try:
            os.lstat(path)
        except FileNotFoundError:
            return None
        raise _RacedEntry(path) from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # The name became a symlink between the lstat and the open: refuse
            # it as the entry-type refusal, never as a bare OSError.
            raise _NonRegularEntry(path) from None
        raise
    try:
        pinned = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if not stat.S_ISREG(pinned.st_mode):
        os.close(fd)
        raise _NonRegularEntry(path)
    if _identity(pinned) != inspected:
        # The handle is a regular file, but not the entry the lstat inspected:
        # another writer re-bound the name inside the window. Nothing this
        # migration could conclude about that file was ever checked.
        os.close(fd)
        raise _RacedEntry(path)
    # The name is inspected AGAIN after the open. Identity equality above is not
    # enough on a platform without O_NOFOLLOW: a symlink swapped in inside the
    # window carries the SAME identity as the handle whenever it points at the
    # file that was expected - which is precisely the alias this module refuses
    # - so no comparison of inodes can see it. What sees it is the entry now at
    # the name: a symlink is not a regular file, so the followed link is refused
    # here instead of being adopted as a completed hardlink.
    try:
        settled = os.lstat(path)
    except FileNotFoundError:
        os.close(fd)
        raise _RacedEntry(path) from None
    if not stat.S_ISREG(settled.st_mode) or _identity(settled) != inspected:
        os.close(fd)
        raise _RacedEntry(path)
    open_fds.append(fd)
    return _identity(pinned)


def _pinned_or_refuse(
    legacy: Path, current: Path, entry: Path, open_fds: list[int]
) -> Identity | None:
    """``_open_pinned`` for ``entry``, with a non-regular entry refused.

    The refusal is the module's ``FileExistsError`` naming both absolute paths,
    not an ``OSError``: a symlinked name is a state the operator reconciles by
    hand, exactly like the two-different-files case. A name re-bound between
    the entry check and the open gets its own wording, since the remedy is the
    same but the thing that was found is not a symlink.
    """
    try:
        return _open_pinned(entry, open_fds)
    except _NonRegularEntry:
        raise _entry_refusal(legacy, current, entry) from None
    except _RacedEntry:
        raise _raced_refusal(legacy, current, entry) from None


def _entry_refusal(legacy: Path, current: Path, entry: Path) -> FileExistsError:
    """The error for "one of the names is not a regular file itself".

    A symlink at either name carries another entry's inode, so identity
    equality between the two names proves nothing about a hardlink that this
    migration was supposed to create. Same type and message shape as the other
    refusals: the operator's remedy is the same (stop, reconcile by hand).
    """
    return FileExistsError(
        errno.EEXIST,
        f"{entry} is a symlink or another non-regular directory entry, not the "
        f"manifest itself, so it cannot stand as proof that {legacy} and {current} "
        f"are one file; nothing was written or deleted here. Put the real file you "
        f"trust under {current.name} and delete the other "
        f"((private path))",
    )


def _raced_refusal(legacy: Path, current: Path, entry: Path) -> FileExistsError:
    """The error for "a name was re-bound between the entry check and the open".

    Not the symlink wording: what the handle pinned is a regular file, just not
    the directory entry that was inspected, so the operator cannot be told the
    name is a symlink. The remedy is the same (stop, reconcile by hand) and the
    message names both absolute paths like every other refusal.
    """
    return FileExistsError(
        errno.EEXIST,
        f"{entry} was replaced by another writer between the entry check and the "
        f"open, so the file this migration is holding is not the directory entry "
        f"it inspected and no identity can be confirmed from it; nothing was "
        f"written or deleted here ({legacy} and {current}). Keep the one you "
        f"trust under {current.name} and delete the other "
        f"((private path))",
    )


def _refusal(legacy: Path, current: Path) -> FileExistsError:
    """The error for "both names exist and they are different files"."""
    return FileExistsError(
        errno.EEXIST,
        f"both {legacy} and {current} exist and are different files; keep the one you "
        f"trust under {current.name} and delete the other "
        f"((private path))",
    )


def _replaced_refusal(legacy: Path, current: Path) -> FileExistsError:
    """The error for "another writer re-bound the legacy name mid-migration".

    Same type and message shape as the both-present refusal: the caller's
    remedy is the same (stop, then reconcile by hand), and a distinct type
    would escape the handlers that already cover the both-present case.
    """
    return FileExistsError(
        errno.EEXIST,
        f"{legacy} was replaced by another writer while the migration ran; nothing "
        f"was deleted here, and the two names are not both the file this migration "
        f"opened ({legacy} and {current}). Keep the one you trust under "
        f"{current.name} and delete the other "
        f"((private path))",
    )


def _vanished_refusal(legacy: Path, current: Path) -> FileExistsError:
    """The error for "a name went away, so the pair cannot be confirmed".

    Nothing is deleted by this migration, so the bytes are always still under
    one of the two names; the refusal exists because reporting success would
    claim a pair of names that is not there to check.
    """
    return FileExistsError(
        errno.EEXIST,
        f"{current} and {legacy} are no longer the two names of one file: one of them "
        f"was removed or replaced by another writer while the migration ran, and "
        f"nothing was deleted here. Keep the manifest you trust under "
        f"{current.name} and delete the other "
        f"((private path))",
    )


def _verify_one_file(legacy: Path, current: Path, opened: Identity) -> None:
    """Refuse unless open handles prove the two names are one file.

    Both names are opened and both identities are read with ``fstat`` from
    those handles, and the handles stay open until the comparison is done, so
    neither side of the comparison is a fresh ``stat`` of a name another writer
    could have re-bound. ``opened`` is the identity the legacy name had before
    the link - also read from a handle - and it is what shows that a surviving
    current name is the file this migration linked when the legacy name is
    removed underneath it.

    Raises ``FileExistsError`` (the module's refusal type) otherwise, always
    naming both absolute paths. Nothing is written or removed here.

    Both names are opened through ``_pinned_or_refuse``, so an entry that is a
    symlink rather than a regular file is a refusal too: without that, a
    symlinked current name would pin the legacy file's identity and this
    function would confirm a migration that never created a directory entry.
    """
    open_fds: list[int] = []
    try:
        pinned_legacy = _pinned_or_refuse(legacy, current, legacy, open_fds)
        pinned_current = _pinned_or_refuse(legacy, current, current, open_fds)
        if pinned_current is None:
            # The current name went away: there is no pair left to confirm, so
            # this may not report success.
            raise _vanished_refusal(legacy, current)
        if pinned_legacy is None:
            if pinned_current == opened:
                # Another writer removed the legacy name. The current name
                # still pins the file this migration opened and linked, so the
                # link is the copy that survives; nothing to reconcile.
                return
            # Legacy removed AND the current name is not the file we opened:
            # neither name can be confirmed any more.
            raise _vanished_refusal(legacy, current)
        if pinned_legacy != opened:
            # The legacy name was re-bound, including in the window between the
            # pre-link pin and the link itself: what either name holds is not
            # the file this migration set out to migrate.
            raise _replaced_refusal(legacy, current)
        if pinned_current != opened:
            # The same legacy file, but a different current file: a manifest
            # that was already there, or one that replaced our link.
            raise _refusal(legacy, current)
    finally:
        for fd in open_fds:
            os.close(fd)


def _link_no_clobber(legacy: Path, current: Path, opened: Identity) -> bool:
    """Create ``current`` as a hardlink to ``legacy``; return True if created.

    ``os.link`` creates the destination exclusively, so the refusal is atomic:
    there is no check-then-act window for a concurrently created manifest to
    slip through, unlike ``Path.rename``/``os.replace`` which silently replace
    the destination on POSIX (and are racy under a preceding ``exists()``).
    ``False`` means an existing ``current`` was confirmed, by open handles, to
    be this same file, which is what a rerun of this migration sees; anything
    else is a refusal from ``_verify_one_file``.

    A filesystem that cannot hardlink raises instead of falling back to a
    rename: a replacing rename is exactly the clobber this guards.
    """
    try:
        os.link(legacy, current)
    except FileExistsError:
        # Decide from handles, not names: the identity pinned before the link
        # and the identity an open handle on ``current`` pins now. This raises
        # unless both names are the file the migration opened, so two
        # independent path stats can never agree by accident here.
        _verify_one_file(legacy, current, opened)
        return False
    except FileNotFoundError:
        # The legacy name was removed between the pre-link pin and the link:
        # that is the documented mid-migration refusal, not a filesystem that
        # cannot hardlink.
        raise _vanished_refusal(legacy, current) from None
    except OSError as exc:
        raise OSError(
            exc.errno,
            f"cannot link {legacy} to {current}: {exc.strerror or exc}; a hardlink is "
            "required so the migration never replaces an existing manifest "
            "((private path))",
        ) from exc
    return True


def migrate_music_manifest(repo_root: Path) -> Path:
    """Give a legacy manifest the current name once; return the current path.

    Legacy only: the current name is created as a hardlink to it, same bytes,
    same inode, and the legacy name is left in place. Neither, or current only:
    no-op. Both names, and both are the file this migration opened: no-op
    (already migrated). The legacy name removed after the link, with the current
    name still pinning the file that was opened: the link is the only copy left,
    so it stands. Every other outcome is FileExistsError naming both absolute
    paths - the two names are different files, the legacy name was re-bound
    before or after the link, the current name was re-bound or removed, either
    name is a symlink or another non-regular directory entry (a symlink
    aliases an inode instead of owning a directory entry, so it is never
    accepted as an already-migrated hardlink), or either name was re-bound
    between the entry check and the open - and nothing is deleted in any of
    them.

    Nothing is ever removed here, so the migration cannot delete a directory
    entry whose bound inode is not the one it linked, and every identity it
    decides on comes from an open handle's ``fstat``. See the module docstring,
    including the writers-stopped precondition that no stdlib POSIX call can
    remove.
    """
    current = repo_root / MUSIC_MANIFEST_REL
    legacy = repo_root / LEGACY_MUSIC_MANIFEST_REL
    # Every descriptor opened here stays open until the call returns. The
    # pre-link pin is not just a remembered (st_dev, st_ino): holding the
    # handle keeps the inode allocated, so a writer cannot unlink it and have
    # the number recycled onto a replacement that then compares equal.
    open_fds: list[int] = []
    try:
        opened = _pinned_or_refuse(legacy, current, legacy, open_fds)
        if opened is None:
            return current

        created = _link_no_clobber(legacy, current, opened)
        _verify_one_file(legacy, current, opened)
        if created:
            # Only after verification, and only on the arm that made the link:
            # the documented outcome is what a completed migration prints, so a
            # run that refuses must not have printed it first. The already
            # migrated arm stays silent.
            print(f"linked {legacy} -> {current} (#5639)")
        return current
    finally:
        for fd in open_fds:
            os.close(fd)
