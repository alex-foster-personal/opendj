"""Directory-fd-anchored path-segment walk -- the TOCTOU-safe primitive.

Split out of :mod:`apps.shared.platform_paths` purely to keep that module
under its file-size ratchet (Amendment 17: extraction into an
already-imported module is the fix for a budget breach, never a shrink of
strings/tests/docs). This module carries no policy of its own about the
share root, ``PathMap``, or ``MappedPath`` -- ``platform_paths.py`` is
its only caller and owns all of that; this module just answers "resolve
this candidate under this root, safely." It takes ``root`` as a plain
argument and never names or imports any live rekordbox constant.

Why this exists (pin from PR #1271 review, packet 9h-T1): the prior
containment guard called ``candidate.resolve(strict=False)`` and THEN
checked ``is_relative_to(root)``. Both steps look at the filesystem, but at
different instants -- a directory inside ``root`` can be replaced by a
symlink pointing outside it AFTER the check runs and BEFORE whatever opens
the file for real. Walking the path one segment at a time and opening each
segment with ``os.open(part, O_RDONLY | O_NOFOLLOW | O_NONBLOCK,
dir_fd=<parent>)`` -- anchored to the fd of the directory already opened
above it, never to a path string re-derived from the root -- makes a
symlink swapped in for ANY segment, at any point during the walk (including
the leaf), raise ``ELOOP`` the instant that segment is opened. There is no
separate "check, then act" step left to race, because the check IS the act
of opening.

Revision note (sol-review v1, BLOCKING P1, same packet): an earlier cut of
this walk passed ``O_DIRECTORY`` on intermediate opens and, on the
``NotADirectoryError`` that produces for a NOFOLLOW-rejected symlinked
directory on macOS, disambiguated it from a legitimate non-symlink file
with a SECOND, name-based ``os.stat(part, dir_fd=..., follow_symlinks=False)``
-- itself a fresh check-then-act pair, in a walk whose entire purpose is
eliminating those. Fixed by dropping ``O_DIRECTORY`` entirely: every segment
opens with plain ``O_NOFOLLOW`` (a symlink there still raises ``ELOOP``,
so the security property is unchanged), and the directory-vs-file
distinction for a non-leaf segment is read off ``os.fstat`` on the FD
ALREADY HELD from that same open -- no second lookup, no new race.
``O_NONBLOCK`` additionally guards every open against blocking forever on a
FIFO a hostile process could plant at that segment.

Second revision note (sol-review v1, BLOCKING P1, packet 9j): the fix above
only covers segments BELOW the root. The root itself was still opened with
plain ``O_DIRECTORY`` and no ``O_NOFOLLOW`` at all -- deliberately, since a
configured root (SHARE_ROOT) is legitimately allowed to be a symlink -- so
replacing the root ITSELF with a symlink to an attacker-controlled
directory made every descendant segment check pass against the wrong
directory; nothing in the per-segment walk below can catch a bad anchor
above it. Fixed with an identity-checked anchor (see ``_verify_root_anchor``
below): the root's ``(st_dev, st_ino)`` is recorded once, the first time
this process trusts it, and every later call re-derives the current
identity and refuses to walk if it no longer matches -- catching a swapped
root while still tolerating one that was a symlink from the start.
"""
from __future__ import annotations

import contextlib
import errno
import logging
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

FD_ANCHORED_WALK_SUPPORTED: bool = (
    sys.platform != "win32"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
)

_IS_DARWIN: bool = sys.platform == "darwin"

# ----- Identity-checked root anchor (sol-review v1 BLOCKING P1, packet 9j) -
#
# ``resolve_under_root`` opens ``root`` itself with plain O_DIRECTORY, no
# O_NOFOLLOW: unlike every other segment in the walk, the root MUST be
# allowed to be a symlink (SHARE_ROOT legitimately is one under remote
# mode -- see ``platform_paths.compute_share_root``), so a blanket
# O_NOFOLLOW there would break that case outright rather than merely
# narrow a race.
#
# But that same tolerance is exactly the P1: a root swapped for a symlink
# to an ATTACKER-CONTROLLED directory between the moment it was last
# trusted and the moment this walk opens it is followed exactly the same
# way, and the walk then anchors below it -- every descendant passes every
# segment check because the checks are all relative to the wrong
# directory. A single ``open()`` cannot tell "trusted, pre-existing
# symlink" apart from "swapped, hostile symlink"; both are just a
# directory fd once opened.
#
# The identity-checked anchor breaks the tie with a second, independent
# fact: WHAT the root resolves to must not have CHANGED since the first
# time this exact process trusted it. The first call for a given literal
# ``root`` path establishes the anchor -- records the ``(st_dev, st_ino)``
# of whatever that root fd's open landed on, exactly once. Every later
# call for that same root re-derives the current identity the same way
# and compares it to the recorded anchor; a mismatch means the path's
# target changed after being trusted, which is precisely a root-swap
# attack, and raises rather than silently walking the new target.
#
# This is deliberately NOT the "AssetResolver carries no module-level
# state" pattern this module's only caller documents for itself -- that
# pattern was rejected because a 30s cache could serve a stale "safe"
# VERDICT without rerunning the check. This cache never skips a check and
# never produces a stale pass: every call still opens and walks the whole
# path fresh, and the anchor can only ever turn a call from "pass" to
# "fail", never the reverse -- it IS the security control, not a shortcut
# around it.
_ROOT_ANCHORS: dict[str, tuple[int, int]] = {}


class RootIdentityChanged(OSError):
    """Raised when a root's on-disk identity no longer matches its anchor.

    Subclasses ``OSError`` so every existing caller that already treats an
    ``OSError`` from the walk as "unsafe, refuse the path" (see
    ``platform_paths._contained_asset_path``) handles this the same way
    with no call-site change required.
    """


def _verify_root_anchor(root: Path, root_fd: int) -> None:
    """Establish (first call) or enforce (later calls) ``root``'s identity anchor.

    Keyed on the literal ``root`` path string a caller supplies -- SHARE_ROOT
    switching library modes (a legitimate, different configured root) gets
    its own independent anchor rather than colliding with a stale one, and
    a root that does not exist yet never reaches here (the caller only
    calls this once ``root_fd`` is already a real, opened directory).
    """
    key = str(root)
    current = os.fstat(root_fd)
    identity = (current.st_dev, current.st_ino)
    anchor = _ROOT_ANCHORS.get(key)
    if anchor is None:
        _ROOT_ANCHORS[key] = identity
        return
    if anchor != identity:
        raise RootIdentityChanged(
            f"root {str(root)!r} no longer resolves to the identity "
            f"anchored at first use (anchored dev={anchor[0]} "
            f"ino={anchor[1]}, now dev={identity[0]} ino={identity[1]}) -- "
            "the root directory was replaced (e.g. with a symlink to an "
            "attacker-controlled location) after being trusted; refusing "
            "to walk it."
        )


def log_root_identity_changed(exc: RootIdentityChanged) -> None:
    """Log a caught :class:`RootIdentityChanged` at ERROR.

    The rich message on the exception was previously constructed and then
    discarded by a bare ``except OSError`` at the only call site
    (``platform_paths._contained_asset_path``) -- this was the sole
    diagnostic the module produced, and it never reached a log.
    """
    log.error(str(exc))


def reset_root_anchor(root: Path) -> None:
    """Drop the recorded anchor for ``root``, so the next call re-anchors.

    For a deliberate library-mode or crate-root change (see
    ``platform_paths.refresh_share_root``), where the new identity is
    legitimately different and should be trusted fresh -- not a general
    escape hatch. The identity check in :func:`_verify_root_anchor` still
    runs on every call after this; only the recorded baseline is cleared.
    """
    _ROOT_ANCHORS.pop(str(root), None)


#: Run by :func:`reset_root_anchors`. Whoever remembers what it read below a
#: root registers its "forget everything" here: a reset says the roots may
#: legitimately name other directories now, so nothing read under the old
#: ones may be served again. Kept in this module, which nothing reloads.
_ANCHOR_RESET_HOOKS: list[Callable[[], None]] = []


def on_root_anchors_reset(forget: Callable[[], None]) -> None:
    """Run ``forget`` every time :func:`reset_root_anchors` runs."""
    if forget not in _ANCHOR_RESET_HOOKS:
        _ANCHOR_RESET_HOOKS.append(forget)


def reset_root_anchors() -> None:
    """Drop every recorded root anchor. See :func:`reset_root_anchor`."""
    _ROOT_ANCHORS.clear()
    for forget in _ANCHOR_RESET_HOOKS:
        forget()


class RootReanchorRefused(OSError):
    """:func:`reanchor_root` would not trust the root; ``str()`` says why."""


def _open_real_directory_in(parent_fd: int, part: str, walked: Path, root: Path) -> int:
    """Open ``part`` below ``parent_fd`` if it is a real directory, never a symlink."""
    try:
        fd = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        why = (
            "is a symbolic link" if exc.errno == errno.ELOOP
            else f"cannot be opened ({type(exc).__name__})"
        )
        raise RootReanchorRefused(
            f"{str(walked)!r}, a directory above the root {str(root)!r}, {why}; "
            "every directory above the root must be a real directory"
        ) from exc
    try:
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
    except BaseException:
        os.close(fd)
        raise
    if not is_directory:
        os.close(fd)
        raise RootReanchorRefused(f"{str(walked)!r}, above the root {str(root)!r}, is not a directory")
    return fd


def _open_real_parent(root: Path) -> int:
    """Open ``root``'s parent directory, refusing a symlink anywhere on the way.

    The walk starts at the filesystem root, the one directory no path can
    re-point, and opens every component below it ``O_NOFOLLOW`` relative to
    the descriptor of the one above. ``O_NOFOLLOW`` on a whole path guards
    only its last component, so opening ``crate/share`` that way follows a
    ``crate`` that was swapped for a symlink to another tree.
    """
    if not root.is_absolute() or ".." in root.parts or len(root.parts) < 2:
        raise RootReanchorRefused(f"{str(root)!r} is not an absolute path below the filesystem root")
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    walked = Path(root.anchor)
    for part in root.parts[1:-1]:
        walked = walked / part
        try:
            child = _open_real_directory_in(fd, part, walked, root)
        finally:
            os.close(fd)
        fd = child
    return fd


def reanchor_root(root: Path) -> bool:
    """Trust ``root`` as the directory it is now. Only ever called on request.

    This is the deliberate reset issue 1402 left to the root's owner: nothing
    that merely reads below a root calls it, so a root that became another
    directory stays refused until someone says the new one is intended.

    Every directory ABOVE the root is opened ``O_NOFOLLOW`` from the
    filesystem root (:func:`_open_real_parent`), and a symlink among them is
    refused with :class:`RootReanchorRefused`, whose text names it. The root
    itself may be a symlink, as a configured share root legitimately is: the
    caller is vouching for where it points now.

    The anchor recorded is the identity of the directory this call opened, not
    of whatever the path names on a later look, under both names the root is
    reached by (the path as given, and its resolved form). Everything
    remembered under the old identity is forgotten (the reset hooks run).

    False, with nothing changed, when the root or a directory above it does
    not exist.
    """
    try:
        parent_fd = _open_real_parent(root)
    except FileNotFoundError:
        return False
    try:
        try:
            root_fd = os.open(root.name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise RootReanchorRefused(
                f"the root {str(root)!r} cannot be opened as a directory ({type(exc).__name__})"
            ) from exc
        try:
            opened = os.fstat(root_fd)
        finally:
            os.close(root_fd)
    finally:
        os.close(parent_fd)
    identity = (opened.st_dev, opened.st_ino)
    _ROOT_ANCHORS[str(root)] = identity
    with contextlib.suppress(OSError, RuntimeError):  # a symlink loop has no second name
        _ROOT_ANCHORS[str(root.resolve())] = identity
    for forget in _ANCHOR_RESET_HOOKS:
        forget()
    log.warning(
        "root %r anchored afresh on request at dev=%d ino=%d", str(root), identity[0], identity[1]
    )
    return True


def path_from_fd(fd: int) -> Path:
    """Recover the real filesystem path an open directory/file fd refers to.

    macOS/BSD have no ``/proc``, so ``fcntl.F_GETPATH`` is the primitive
    there; Linux exposes the same information via ``/proc/self/fd/<fd>``.
    """
    if _IS_DARWIN:
        import fcntl

        buf = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\x00" * 1024)
        return Path(buf.split(b"\x00", 1)[0].decode())
    return Path(os.readlink(f"/proc/self/fd/{fd}"))


def resolve_under_root(candidate: Path, root: Path) -> Path:
    """Resolve ``candidate`` via a directory-fd-anchored walk from ``root``.

    ``candidate`` must be ``root`` itself or a lexical descendant of it --
    raises ``ValueError`` otherwise (the caller decides what that means; this
    function has no opinion about alternate root forms).

    Raises ``OSError`` the moment any segment -- directory or leaf -- turns
    out to be a symlink, at the exact instant it is opened: every segment is
    opened with plain ``O_NOFOLLOW`` (no ``O_DIRECTORY``), so a symlink
    there raises ``ELOOP`` regardless of whether a directory or a file was
    expected. Whether a non-leaf segment is actually a directory is then
    read off ``os.fstat`` on the FD JUST OPENED -- never a second, name-based
    lookup, which would reopen the exact check-then-act gap this walk exists
    to close (sol-review v1 P1, see module docstring).

    A missing tail component, or a non-leaf segment that turns out to be a
    real, non-symlink non-directory (a plain file, FIFO, etc. -- the
    ``O_NONBLOCK`` on the open guards against blocking forever on a
    hostile-planted FIFO), is tolerated exactly like ``resolve(strict=False)``
    was: the unresolved remainder is appended lexically onto the last
    directory fd's real, symlink-verified path, rather than being treated as
    an escape attempt.

    Also raises ``RootIdentityChanged`` (an ``OSError`` subclass) the first
    time ``root`` itself is opened after having been swapped for a symlink
    to somewhere else since this process last trusted it -- see the module
    docstring's second revision note and ``_verify_root_anchor``. ``root``
    is still allowed to be a symlink outright (SHARE_ROOT legitimately is,
    under remote mode); only a CHANGE in what it resolves to, after the
    fact, is rejected.
    """
    relative_parts = candidate.relative_to(root).parts
    if ".." in relative_parts:
        # sol-review v1 BLOCKING P1 (packet 9i-1): Path.relative_to() does
        # NOT normalize '..' -- a candidate shaped like root/../outside/file
        # still starts with root's own parts lexically, so relative_to()
        # succeeds and hands back parts beginning with '..'. Left unchecked,
        # the walk below would os.open('..', dir_fd=root_fd), stepping to
        # root's own parent and out of containment before a single symlink
        # check ever runs. Reject it here, lexically, before opening
        # anything -- including before the root fd itself is opened.
        raise ValueError(
            f"{candidate!r} contains a '..' component relative to "
            f"{root!r} -- lexical escape, not a descendant of root"
        )
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except (FileNotFoundError, NotADirectoryError):
        # root itself does not exist yet -- not an escape attempt, nothing
        # to walk. Mirror resolve(strict=False)'s tolerance.
        return root.joinpath(*relative_parts)
    opened_fds = [root_fd]
    try:
        _verify_root_anchor(root, root_fd)
        current_fd = root_fd
        for index, part in enumerate(relative_parts):
            is_last = index == len(relative_parts) - 1
            try:
                fd = os.open(
                    part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current_fd
                )
            except FileNotFoundError:
                # Not an escape attempt -- the segment (and everything
                # after it) simply does not exist yet. The returned suffix
                # is a LEXICAL, non-fd-verified path: never to be confused
                # with "file existed and got swapped" (that raises ELOOP
                # above, uncaught here, and propagates to the caller).
                base = path_from_fd(current_fd)
                return base.joinpath(*relative_parts[index:])
            opened_fds.append(fd)
            if not is_last:
                # A real, non-symlink non-directory sitting where a
                # directory was expected is legitimate data (resolve
                # (strict=False) tolerated this too) -- checked on the fd
                # we already hold, not by re-deriving the name. Stop
                # walking and hand back the unresolved remainder lexically,
                # same as a missing tail.
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    base = path_from_fd(fd)
                    return base.joinpath(*relative_parts[index + 1 :])
            current_fd = fd
        return path_from_fd(current_fd)
    finally:
        for fd in reversed(opened_fds):
            os.close(fd)


# ----- reading through the walk's own descriptors (LIBM-137 round 3) ----------
#
# ``resolve_under_root`` hands back a PATH, and its caller opens that path
# afterwards: the walk proves containment at the instant it ran, and a swap
# between the walk and that later open is outside it. That is tolerable for one
# request and wrong for anything that REMEMBERS what it read, because a race
# won once would then be served on every later request.
#
# The functions below close that gap for a caller that wants bytes rather than
# a path. The root is opened and identity-checked exactly as above, every
# directory segment is opened ``O_NOFOLLOW`` relative to the descriptor of its
# parent, and the leaf is opened ``O_NOFOLLOW`` relative to the descriptor of
# its directory and READ FROM THAT DESCRIPTOR. No path string is ever reopened,
# so there is no instant at which a swapped segment can redirect the read.

#: ``(st_mode, st_ino, st_dev, st_size, st_mtime_ns, st_ctime_ns)``.
Identity = tuple[int, int, int, int, int, int]

_LEAF_FLAGS: int = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK if FD_ANCHORED_WALK_SUPPORTED else 0
_READ_CHUNK: int = 1 << 20


def identity_of(st: os.stat_result) -> Identity:
    """What one inode looked like: type, which inode, and its content stamps.

    ``st_ctime_ns`` is in because ``st_mtime_ns`` can be put back by whoever
    rewrote the file, and the change time cannot.
    """
    return (st.st_mode, st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _require_plain_segment(name: str) -> None:
    """Refuse anything that is not one ordinary path component.

    ``os.open("a/b", dir_fd=fd)`` resolves ``a`` the ordinary way, following a
    symlink there, and ``..`` steps out of the directory the descriptor names.
    Either would undo the walk, so both are refused before any open.
    """
    if name in ("", ".", "..") or "/" in name or "\x00" in name:
        raise ValueError(f"{name!r} is not a single plain path component")


def open_anchored_root(root: Path) -> int | None:
    """Open ``root`` and enforce its identity anchor; None when it does not exist.

    Raises :class:`RootIdentityChanged` exactly as :func:`resolve_under_root`
    does. The caller owns the descriptor and closes it.
    """
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        _verify_root_anchor(root, root_fd)
    except BaseException:
        os.close(root_fd)
        raise
    return root_fd


def open_directory_under(
    root_fd: int, parts: tuple[str, ...]
) -> tuple[int | None, list[os.stat_result]]:
    """Walk directory ``parts`` below an opened root, one descriptor at a time.

    Returns ``(dir_fd, stats)``: the descriptor of the last directory, which
    the caller closes, and the ``fstat`` of every segment that opened. The
    descriptor is None when a segment is missing or is a real non-directory,
    which is ordinary data and not an escape (``stats`` then stops there).

    Raises ``OSError`` when a segment is a symlink (``ELOOP``) or cannot be
    opened for any other reason, and ``ValueError`` for a segment that is not
    one plain component.
    """
    for part in parts:
        _require_plain_segment(part)
    stats: list[os.stat_result] = []
    current_fd = root_fd
    try:
        for part in parts:
            try:
                fd = os.open(part, _LEAF_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if current_fd != root_fd:
                    os.close(current_fd)
                return None, stats
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = fd
            st = os.fstat(fd)
            stats.append(st)
            if not stat.S_ISDIR(st.st_mode):
                os.close(fd)
                return None, stats
    except BaseException:
        if current_fd != root_fd:
            os.close(current_fd)
        raise
    if current_fd == root_fd:
        return os.dup(root_fd), stats
    return current_fd, stats


def stat_leaf(dir_fd: int, name: str) -> os.stat_result | None:
    """``lstat`` of ``name`` inside an opened directory; None when it is absent.

    Never follows a final symlink: the caller sees ``S_ISLNK`` and decides.
    """
    _require_plain_segment(name)
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def read_leaf(dir_fd: int, name: str, max_bytes: int) -> tuple[os.stat_result, bytes | None] | None:
    """Open ``name`` inside an opened directory and read it from that descriptor.

    None when it is absent. Otherwise ``(fstat, data)``, where the ``fstat`` is
    taken on the descriptor BEFORE the read, so a write that lands during the
    read shows up as a changed file to whoever compares it later. ``data`` is
    None for anything that is not a regular file (never read: a FIFO would
    block or lie), for a file larger than ``max_bytes``, and for a file with
    more than one name (``st_nlink > 1``): a hard link is the one way a name
    inside the root can be an inode that also lives outside it, and no symlink
    check sees it. Real rekordbox trees hold none (0 in 81,075 files surveyed).

    Raises ``OSError`` when the leaf is a symlink (``ELOOP``) or cannot be
    opened or read.
    """
    _require_plain_segment(name)
    try:
        fd = os.open(name, _LEAF_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1 or st.st_size > max_bytes:
            return st, None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                return st, None
            chunks.append(chunk)
        return st, b"".join(chunks)
    finally:
        os.close(fd)
