"""Where the numba JIT warm-up lock and stamp live, and how they are opened.

Split out of ``apps.analysis.jit_warmup`` (issue #1401) so that module stays
under the repo's 600-line ceiling. Placement is also its own concern: where the
lock lives is a security decision, not a convenience one.

Why. The warm-up serializes compiles into ONE on-disk numba cache that on a CI
host is shared by processes that are not always the same UNIX user - agentbox
has run the same venv as both ``root`` and ``ghrunner``. Placing a predictable
lock file in the world-writable temp root let any local user pre-plant a
symlink there (an arbitrary-file chmod when the victim ran as root) or an
unopenable file (which made every warm-up run UNLOCKED - the condition
documented as corrupting the cache permanently). So the lock and stamp now
live in a directory this module first makes safe:

* By default a ``mdt-numba-warmup-<key>`` directory under the temp root,
  created 0700 and private to the user who created it. Cross-user sharing is
  retired unless the operator opts in.
* The opt-in is ``MDT_NUMBA_WARMUP_DIR``: a directory the operator controls,
  set identically on every user who must serialize. It is used as-is; this
  module never creates or chmods it.
* Whatever the directory, the lock file is opened with ``O_NOFOLLOW`` and used
  only when ``fstat`` reports a regular file (owned by this user unless the
  operator directory is in play). There is no unconditional ``fchmod``: the
  lock file holds no contents, and chmodding a path we did not safely create
  is the primitive the symlink attack abused.

A lock that cannot be made trustworthy makes the warm-up refuse, never run
unlocked. Callers treat ``False`` from ``_ensure_lock_dir`` and ``None`` from
``_open_lock_safe`` as fail-closed.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import logging
import os
import stat
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

log = logging.getLogger("apps.analysis.jit_warmup")


def _env_key() -> str:
    """Identify the numba cache this process would write.

    Keyed on what determines the cache location: the interpreter prefix
    (whose ``site-packages`` numba writes beside) and ``NUMBA_CACHE_DIR``
    (which overrides that when set). Two processes share a lock and a stamp
    exactly when they would share a cache.
    """
    return hashlib.sha256(
        "\0".join(
            [
                sys.prefix,
                sys.base_prefix,
                os.environ.get("NUMBA_CACHE_DIR", ""),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]


#: Env var naming a directory the operator controls, where the lock and stamp
#: for every cache live. Unset, each user gets a private 0700 directory and a
#: lock serializes only that user's processes. Setting it - to the SAME
#: directory, on every sharing user - opts back INTO the cross-user
#: serialization the original world-writable-temp placement provided, at the
#: cost of trusting that directory (issue #1401).
LOCK_DIR_ENV = "MDT_NUMBA_WARMUP_DIR"


def _lock_dir_override() -> str | None:
    """``MDT_NUMBA_WARMUP_DIR``, or ``None`` when unset OR empty.

    The single source of truth for "did the operator opt into a shared
    directory". An empty string (``MDT_NUMBA_WARMUP_DIR=``) is treated as
    unset rather than as an opt-in to sharing an empty path - anything else
    would compile against ``warmup_dir()``'s own truthiness check, giving the
    private default path a shared-mode ownership check (issue #1401).
    """
    return os.environ.get(LOCK_DIR_ENV) or None


def warmup_dir() -> Path:
    """Directory holding the lock and stamp for THIS cache.

    ``MDT_NUMBA_WARMUP_DIR`` when set (the explicit opt-in for cross-user
    sharing); otherwise a ``mdt-numba-warmup-<key>`` directory under the temp
    root, created 0700 and private to whichever user first ran here.
    """
    override = _lock_dir_override()
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / f"mdt-numba-warmup-{_env_key()}"


def _is_owned_dir(path: Path) -> bool:
    """True when ``path`` is a real directory (no symlink hop) owned by us.

    ``lstat``, not ``stat``: a symlink must fail this check on its own link
    rather than resolving to whatever it points at, or a planted symlink into
    someone else's directory would read as ours.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()


def _ensure_lock_dir(path: Path, *, shared: bool) -> bool:
    """Make ``path`` safe to lock inside; False means the caller fails closed.

    Private mode creates it 0700 and REFUSES anything already there that is not
    a real directory owned by this user - a planted symlink or another user's
    directory where our 0700 directory should be means no trustworthy lock
    exists. Shared mode only requires a real (non-symlink) directory, because
    the operator who named it vouches for it.
    """
    if shared:
        try:
            st = os.lstat(path)
        except OSError:
            return False
        return stat.S_ISDIR(st.st_mode)
    with suppress(FileExistsError):
        os.mkdir(path, 0o700)
    return _is_owned_dir(path)


#: Sentinel file proving a directory is dedicated to this cache, planted the
#: first time this module claims an empty one. No ``.nbi``/``.nbc`` suffix,
#: so ``purge_cache`` never removes it - dedication is a property of the
#: directory's identity, not of what currently happens to be in it.
_CACHE_DEDICATION_MARKER = ".mdt-numba-cache-owned"


def _is_dedicated_cache_dir(path: Path) -> bool:
    """True when ``path`` is confirmed dedicated to this cache, not shared.

    Ownership alone does not make a directory safe to ``purge_stale`` into:
    ``purge_cache`` deletes every ``*.nbi``/``*.nbc`` anywhere under its root,
    so an operator's general-purpose ``NUMBA_CACHE_DIR`` - shared with other
    numba-using tools - would lose their artifacts too (issue #1572 review).
    An empty directory is claimed by planting the marker. A non-empty one is
    trusted when the marker is already there, OR when ``warmup_stamp_path()``
    already exists for this exact ``NUMBA_CACHE_DIR`` - proof this module's
    own warm-up machinery already vouched for it, even through an entrypoint
    that never calls this function: CI pre-warms a persistent per-runner
    cache with the standalone ``jit_warmup`` CLI directly (issue #1437),
    which writes real artifacts and a stamp without ever planting our
    marker. Anything else - real content, no marker, no stamp for this
    exact configuration - is an operator's general-purpose numba cache we
    have never touched, and is refused.
    """
    marker = path / _CACHE_DEDICATION_MARKER
    if marker.exists():
        return True
    if warmup_stamp_path().exists():
        marker.touch()
        return True
    if any(path.iterdir()):
        return False
    marker.touch()
    return True


def ensure_owned_numba_cache_dir() -> Path | None:
    """Make ``NUMBA_CACHE_DIR`` name a directory this process may purge.

    Unset: create a private 0700 ``mdt-numba-cache-<uid>-<key>`` directory
    under the temp root (the same key ``warmup_dir()`` uses, computed before
    this call can change what it hashes) and publish it into ``os.environ``
    so both this module's own cache lookups and numba's own internal caching
    agree on it - every deployment gets purge protection, not only the ones
    that happen to set the variable themselves. The UID is part of the name:
    without it, a second user sharing this venv (agentbox has run it as both
    root and ghrunner) finds the first user's directory, fails the ownership
    check below, and silently loses purge protection instead of getting a
    directory of their own (issue #1572 review).

    Set by the operator: verified the same way ``_ensure_lock_dir`` verifies
    the private warm-up directory - a real directory (no symlink hop), owned
    by this user - rather than trusted for merely being present. A missing
    operator directory is refused, not created: it is theirs to make, exactly
    like ``MDT_NUMBA_WARMUP_DIR``.

    Either way, also checked against ``_is_dedicated_cache_dir``: an owned
    directory that already holds something else's files is refused too,
    because ownership says nothing about whether it is safe to delete
    numba's own artifacts from that root without touching anyone else's.

    Returns ``None`` (fail closed, caller must not purge) when either check
    fails.
    """
    configured = os.environ.get("NUMBA_CACHE_DIR")
    if configured:
        path = Path(configured)
    else:
        path = Path(tempfile.gettempdir()) / f"mdt-numba-cache-{os.getuid()}-{_env_key()}"
        with suppress(FileExistsError):
            os.mkdir(path, 0o700)
    if not _is_owned_dir(path) or not _is_dedicated_cache_dir(path):
        return None
    if not configured:
        os.environ["NUMBA_CACHE_DIR"] = str(path)
    return path


def warmup_stamp_path() -> Path:
    """Where the fingerprint of the last successful serial warm-up is kept.

    Inside the lock's directory, so both move together when the operator opts
    into a shared directory. Written via a temp file and ``os.replace`` in
    ``_write_stamp``, so a reader never sees a half-written stamp.
    """
    return warmup_dir() / f"mdt-numba-warmup-{_env_key()}.stamp"


def warmup_lock_path() -> Path:
    """Where the compile lock for THIS environment's numba cache lives.

    Keyed on what determines the cache location - the interpreter prefix and
    ``NUMBA_CACHE_DIR`` - and placed inside ``warmup_dir()``, so processes
    share a lock exactly when they share a cache AND (by default) a user.
    Cross-user sharing needs the explicit ``MDT_NUMBA_WARMUP_DIR`` opt-in, so
    a predictable lock path in the world-writable temp root is gone.
    """
    return warmup_dir() / f"mdt-numba-warmup-{_env_key()}.lock"


def _open_lock_safe(path: Path, *, require_self_owned: bool) -> int | None:
    """Open ``path`` for flocking only if it is a safe regular file; else None.

    ``O_NOFOLLOW`` refuses a planted symlink outright. After the open, an fd is
    accepted only when ``fstat`` reports a regular file - and, unless the
    operator opted into a shared directory, one owned by this user. Anything
    else (a FIFO, a device, another user's file) is refused. There is no
    ``fchmod`` here and none is wanted: nothing reads the lock file's contents,
    and chmodding a path we did not safely create is the primitive the symlink
    attack abused. A second trusted user takes the same lock by opening the
    file read-only, which is enough for ``flock``.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o666)
    except OSError:
        # Exists but not writable by us (another trusted user created it in a
        # shared directory), or a symlink O_NOFOLLOW refused. Retry read-only:
        # flock needs only a readable fd; a symlink is refused again.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(st.st_mode) or (
        require_self_owned and st.st_uid != os.getuid()
    ):
        os.close(fd)
        return None
    return fd


#: Distributions whose upgrade invalidates every numba artifact on disk.
TOOLCHAIN_DISTRIBUTIONS: tuple[str, ...] = ("numba", "llvmlite", "librosa", "numpy")


def toolchain_identity() -> str:
    """What compiled the artifacts: interpreter and JIT toolchain versions.

    Read from dist-info, not by importing numba or librosa, so it is cheap
    enough to compute on every CLI start. A distribution that is not
    installed reads as ``absent``, which is itself an identity: a cache
    compiled with librosa present must not vouch for a venv without it.
    """
    parts = [f"py{sys.version_info[0]}.{sys.version_info[1]}"]
    for name in TOOLCHAIN_DISTRIBUTIONS:
        try:
            parts.append(f"{name}{importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            parts.append(f"{name}absent")
    return "-".join(parts)
