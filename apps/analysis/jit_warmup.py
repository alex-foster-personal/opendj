"""Serialized warm-up of the analysis backend's on-disk JIT cache.

Why this module exists (issue #1316)
------------------------------------

librosa marks several of its hot paths ``@numba.guvectorize(..., cache=True)``
- ``librosa.util.utils.peak_pick`` and ``librosa.beat.__last_beat`` among them.
A ``cache=True`` function compiles on first call and writes the result to
``.nbi``/``.nbc`` files beside the librosa source. Every later process reads
them instead of recompiling.

Those writes are NOT safe against a second process compiling the same function
at the same time. When several ``apps.analysis.run`` processes meet a cold
cache together they interleave their writes, and the index and the object file
stop agreeing. From that point on every process that LOADS the cache - not just
the ones that raced - jumps into a partially-mapped object and dies at a NULL
instruction pointer:

    kernel: python3[...]: segfault at 0 ip 0000000000000000 ... in python3.11

There is no Python-level exception for that, so a pool reports only
``BrokenProcessPool`` and a single-process run reports only exit 139. It is
permanent until the artifacts are deleted: measured on agentbox, 12 concurrent
cold-cache processes poisoned the cache, and 5 later SEQUENTIAL runs against it
were 5/5 dead. The same cache warmed once serially first survived 28/28
concurrent runs.

Neither ``fork`` nor the process pool is involved: the crash reproduces with
``--workers 1``, which creates no child process at all.

What this module does
---------------------

Compile once, in the parent, before any concurrency exists. Two processes can
still meet a cold cache (two e2e jobs on one host, two app instances, a
developer's first run racing a background drain), and the parent of one cannot
serialize the other, so the warm-up itself is taken under an advisory
cross-process file lock. Whoever gets there first compiles; everyone else
waits and then finds a complete cache.

The lock and its stamp live in a private 0700 directory under the temp root,
keyed on the cache, so a local user cannot pre-plant a predictable path in the
world-writable temp root and turn the lock into an arbitrary-file chmod, or
into a reason to warm a cold cache unlocked. Cross-user sharing (the venv has
been driven as both ``root`` and ``ghrunner`` on agentbox) survives only by
explicit opt-in: set ``MDT_NUMBA_WARMUP_DIR`` to a directory you control, on
every user who must serialize. When no trustworthy lock is possible the warm-up
is REFUSED, never run unlocked: an unlocked compile of a cold shared cache is
exactly the corruption this module exists to prevent (issue #1401).

Rejected alternative: a per-invocation ``NUMBA_CACHE_DIR``. It removes the
race by removing the sharing, and was measured at 6.1s warm against 35.4s
cold, i.e. +29s on EVERY invocation of a chunked drain. A lock is paid once,
by one process, on a cold cache only.

Not paying it twice
-------------------

Driving those entry points costs ~6.5s even against a complete cache on an
M-series Mac, almost all of it recompiling librosa's ``__beat_local_score``,
which is declared ``cache=False`` and therefore compiles fresh in every
process no matter what. The webui drain invokes this CLI once per chunk of
two tracks, so paying that per invocation is a large regression on the very
path this is meant to protect.

So the warm-up is skipped when the on-disk cache is byte-for-byte what the
last successful serial warm-up left behind. That is checked by fingerprinting
the ``*.nbi``/``*.nbc`` artifacts (path and content) under the backend's
cache roots and comparing against a stamp file written at the end of a
warm-up. The invariant it asserts is exactly the one that matters - "this
cache was produced by a serial compile and nothing has written to it since" -
and anything else, including a purge, a librosa upgrade, or a racing writer,
changes the fingerprint and warms again under the lock. Content, not path,
size and mtime: a torn or interleaved write from a racing or killed writer
can rewrite an artifact's bytes in place at its original length and mtime
(issue #1572), which a size/mtime-only fingerprint cannot see.

It is verified, not assumed, that a warm-up leaves nothing for the workers to
compile: on a purged cache the warm-up writes 78 artifacts, and a subsequent
two-worker run over real mp3 fixtures leaves that set unchanged. If that ever
stops holding, the fingerprint moves every run and the fast path stops firing
- it degrades to the unconditional warm-up rather than to a silent race.

What this does NOT do: repair a cache that is already corrupt. Loading a
corrupt entry is what segfaults, so no in-process code runs after it. Purging
``*.nbi``/``*.nbc`` is the repair, which is why CI does it as workspace
hygiene rather than relying on this module.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ._warmup_lock import (
    LOCK_DIR_ENV,
    _ensure_lock_dir,
    _lock_dir_override,
    _open_lock_safe,
    toolchain_identity,
    warmup_lock_path,
    warmup_stamp_path,
)


class JitWarmable(Protocol):
    """The two class methods this module needs, and deliberately no more.

    Narrower than the full analyzer-backend protocol on purpose. Warming a JIT
    cache has nothing to do with analyzing a track, so demanding a whole
    backend here would force every probe and every future caller to carry an
    ``analyze`` this module never calls. The shipped backends satisfy this
    structurally, so nothing about them changes.
    """

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        ...  # pragma: no cover

    @classmethod
    def warm_jit_cache(cls) -> str:
        ...  # pragma: no cover


log = logging.getLogger("apps.analysis.jit_warmup")

#: Stable prefix of the one line the warm-up prints. Callers grep for it and
#: tests assert on it, so it is part of this module's contract, not decoration.
WARMUP_LINE_PREFIX = "jit-warmup"

#: How long a process waits for another process's cold compile before giving
#: up on the lock. A cold librosa compile is ~30s on a loaded CI host; five
#: minutes is several of those and means something is wrong rather than slow.
#: On timeout the warm-up still runs (serially, in this process) - it just
#: loses the cross-process guarantee, and says so at ERROR.
LOCK_TIMEOUT_S: float = 300.0

_LOCK_POLL_S: float = 0.25


@dataclass(frozen=True)
class WarmupResult:
    """What one warm-up did, for logging and for tests to assert on."""

    backend: str
    seconds: float
    lock_path: Path
    lock_held: bool
    waited_s: float
    detail: str
    #: True when the cache was already what a previous serial warm-up left,
    #: so nothing was compiled. Reported rather than hidden: a run that
    #: skipped and a run that compiled are different facts about the host.
    skipped: bool = False
    #: True when the warm-up was REFUSED outright because no trustworthy lock
    #: could be established (a hostile or unusable lock directory or file,
    #: issue #1401). Nothing was compiled - an unlocked compile of a cold
    #: shared cache is the corruption this module exists to prevent.
    refused: bool = False
    #: Artifacts deleted under the lock because the stamp did not vouch for
    #: them (``purge_stale``). Zero on a stamped or an empty cache.
    purged: int = 0

    def render(self) -> str:
        if self.refused:
            return (
                f"{WARMUP_LINE_PREFIX} backend={self.backend} "
                f"seconds={self.seconds:.2f} refused=1 {self.detail}"
            )
        if self.skipped:
            return (
                f"{WARMUP_LINE_PREFIX} backend={self.backend} "
                f"seconds={self.seconds:.2f} skipped=1 {self.detail}"
            )
        lock = "locked" if self.lock_held else "UNLOCKED"
        return (
            f"{WARMUP_LINE_PREFIX} backend={self.backend} "
            f"seconds={self.seconds:.2f} lock={lock} purged={self.purged} "
            f"waited_s={self.waited_s:.2f} {self.detail}"
        )


def cache_fingerprint(roots: Sequence[Path], identity: str | None = None) -> str:
    """Fingerprint the JIT cache artifacts under ``roots`` and what compiled them.

    Empty string when ``roots`` is empty, when no artifact exists, or when a
    root cannot be walked. Every one of those means "I cannot vouch for this
    cache", and the caller treats an empty fingerprint as never matching a
    stamp, so the failure mode is an extra warm-up rather than a skipped one.

    Content, not size and mtime (issue #1572): a torn or interleaved write
    from a racing or killed compiler can leave an artifact the wrong bytes at
    its ORIGINAL length and mtime - the writer opened the existing file in
    place, so nothing about the directory entry moved. A size-and-mtime
    fingerprint vouches for that cache anyway, the warm-up skips, and the
    next loader dereferences the corrupt object code: the exact #1316 crash
    this module exists to prevent, on a cache the stamp swore was fine.

    Measured on a real 78-artifact / ~4 MB librosa cache (the same shape as
    CI's persistent per-runner cache): content-hashing it costs ~0.006 s,
    against a 25-45 s cold compile. The "tens of megabytes" this docstring
    used to warn about does not describe this cache; the warm-up this
    fingerprint gates already dwarfs the cost of reading what it produced.

    ``identity`` (default :func:`toolchain_identity`) is folded in: a cache
    that persists across venv recreation outlives a librosa or numba upgrade
    that touches no artifact, and without it the stamp would still vouch,
    the warm-up would skip, and numba would reject the artifacts at first
    use in whichever processes got there first, concurrently and cold.
    """
    if identity is None:
        identity = toolchain_identity()
    try:
        artifacts = _cache_artifacts(roots)
        entries = [
            f"{path}\0{hashlib.sha256(path.read_bytes()).hexdigest()}" for path in artifacts
        ]
    except OSError as exc:
        log.error(
            "jit-warmup: cannot fingerprint cache roots %s (%s); warming "
            "unconditionally",
            [str(r) for r in roots],
            exc,
        )
        return ""
    if not entries:
        return ""
    digest = hashlib.sha256("\n".join([identity, *entries]).encode("utf-8")).hexdigest()
    return f"{len(entries)}:{digest}"


def _cache_artifacts(roots: Sequence[Path]) -> list[Path]:
    """Every JIT cache artifact under ``roots``, sorted.

    One definition of "artifact", shared by the fingerprint, the purge and
    the post-warm count, so those three can never disagree about what they
    are talking about.
    """
    found: list[Path] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix in (".nbi", ".nbc") and path.is_file():
                found.append(path)
    return found


def _read_stamp(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_stamp(path: Path, fingerprint: str) -> None:
    """Record ``fingerprint`` as "this cache came from a serial warm-up".

    Written via a temp file in the same directory and renamed, so a reader
    never sees a half-written stamp. A failure to write is logged and
    tolerated: it costs a warm-up next time, which is the safe direction.
    """
    if not fingerprint:
        return
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), delete=False
        ) as fh:
            fh.write(fingerprint)
            tmp = Path(fh.name)
        os.chmod(tmp, 0o666)
        os.replace(tmp, path)
    except OSError as exc:
        log.error(
            "jit-warmup: cannot write stamp %s (%s); the next run will warm "
            "again rather than trust an unverified cache",
            path,
            exc,
        )


@contextmanager
def _compile_lock(
    path: Path, timeout_s: float, *, require_self_owned: bool
) -> Iterator[tuple[bool, bool, float]]:
    """Hold an exclusive advisory lock on ``path``; yield (held, proceed, waited_s).

    ``proceed`` is whether the caller may run the compile. It is False only
    when the lock file itself could not be opened or verified as a safe regular
    file - a planted symlink, FIFO, or unopenable file at a predictable path
    (issue #1401). There the warm-up is REFUSED rather than run unlocked:
    compiling a cold shared cache without the cross-process lock is the state
    documented as corrupting it permanently. A lock-hold timeout or a missing
    ``fcntl`` still yields ``proceed=True`` - degraded and logged at ERROR, but
    the caller may warm, as before.

    ``flock`` is released by the kernel when the fd closes, including when the
    holder is killed, so a dead process cannot leave a stale lock behind.
    """
    started = time.monotonic()
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX (Windows)
        log.error(
            "jit-warmup: fcntl unavailable on %s; warming WITHOUT a "
            "cross-process lock, so a concurrent cold compile can still "
            "corrupt the numba cache",
            sys.platform,
        )
        yield False, True, 0.0
        return

    fd = _open_lock_safe(path, require_self_owned=require_self_owned)
    if fd is None:
        log.error(
            "jit-warmup: lock %s is not a safe regular file this process may "
            "open; warm-up REFUSED rather than compile a cold cache unlocked "
            "(#1401)",
            path,
        )
        yield False, False, time.monotonic() - started
        return

    try:
        deadline = started + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    log.error(
                        "jit-warmup: another process held %s for more than "
                        "%.0fs; warming WITHOUT the cross-process lock",
                        path,
                        timeout_s,
                    )
                    yield False, True, time.monotonic() - started
                    return
                time.sleep(_LOCK_POLL_S)
        waited = time.monotonic() - started
        try:
            yield True, True, waited
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------


def _require_backend_methods(
    backend: type[JitWarmable], backend_name: str
) -> None:
    """Refuse a backend that has not declared how it warms, with a named error.

    A backend missing either method used to die with a bare `AttributeError:
    type object 'X' has no attribute 'jit_cache_roots'` from inside the
    warm-up, which reads as a bug in this module rather than as "that backend
    has not declared whether it has a JIT cache". It is not defaulted, because
    a silent `()` would make an undeclared backend take the "nothing to
    protect" path, which is exactly the wrong guess.
    """
    missing = [
        name
        for name in ("jit_cache_roots", "warm_jit_cache")
        if not hasattr(backend, name)
    ]
    if missing:
        raise TypeError(
            f"backend {backend_name!r} ({backend!r}) does not implement "
            f"{missing}; every analyzer backend must say whether it writes a "
            "numba JIT cache and how to warm it, because two processes "
            "compiling into one shared cache corrupt it (issue #1316). A "
            "backend with no in-process JIT returns () and a short string "
            "saying so - see MikBackend."
        )


def warm_backend_jit(
    backend: type[JitWarmable],
    *,
    backend_name: str,
    timeout_s: float = LOCK_TIMEOUT_S,
    purge_stale: bool = False,
) -> WarmupResult:
    """Compile ``backend``'s cached JIT paths once, under the compile lock.

    ``purge_stale`` deletes the artifacts on disk before compiling when, INSIDE
    the lock, the stamp does not vouch for them: a persistent per-runner cache
    a killed or racing writer touched, or one a toolchain upgrade left behind,
    must never be loaded. Under the lock, so no process can delete what the
    lock holder is writing; the CLI's ``--purge-if-stale`` is this flag.

    Always runs, for every worker count. ``--workers 1`` is not exempt: the
    corruption needs two PROCESSES, not two workers, and one CLI invocation
    per chunk is exactly two processes when a drain and a refresh overlap.
    """
    _require_backend_methods(backend, backend_name)

    lock_path = warmup_lock_path()
    stamp_path = warmup_stamp_path()
    roots = tuple(backend.jit_cache_roots())

    checked = time.monotonic()
    fingerprint = cache_fingerprint(roots)
    if fingerprint and fingerprint == _read_stamp(stamp_path):
        return WarmupResult(
            backend=backend_name,
            seconds=time.monotonic() - checked,
            lock_path=lock_path,
            lock_held=False,
            waited_s=0.0,
            detail=f"cache unchanged since last serial warm-up ({fingerprint})",
            skipped=True,
        )

    # The lock's directory is the security boundary (issue #1401). Refuse the
    # warm-up rather than compile a cold cache unlocked when it is not safe.
    shared = _lock_dir_override() is not None
    if not _ensure_lock_dir(lock_path.parent, shared=shared):
        log.error(
            "jit-warmup: lock directory %s cannot be made trustworthy; warm-up "
            "REFUSED rather than compile a cold cache unlocked (#1401)",
            lock_path.parent,
        )
        return WarmupResult(
            backend=backend_name,
            seconds=time.monotonic() - checked,
            lock_path=lock_path,
            lock_held=False,
            waited_s=0.0,
            refused=True,
            detail=(
                f"lock directory {lock_path.parent} is not trustworthy; "
                "warm-up refused"
            ),
        )

    with _compile_lock(
        lock_path, timeout_s, require_self_owned=not shared
    ) as (held, proceed, waited):
        # Re-check inside the lock. The common cold-start shape is several
        # processes arriving together; whoever loses the race must not
        # recompile what the winner just finished writing.
        if held:
            fingerprint = cache_fingerprint(roots)
            if fingerprint and fingerprint == _read_stamp(stamp_path):
                return WarmupResult(
                    backend=backend_name,
                    seconds=0.0,
                    lock_path=lock_path,
                    lock_held=True,
                    waited_s=waited,
                    detail=(
                        "another process warmed this cache while we waited "
                        f"({fingerprint})"
                    ),
                    skipped=True,
                )
        if not proceed:
            # The lock file could not be opened or verified as a safe regular
            # file. Compiling anyway is the unlocked warm-up a hostile lock
            # exists to provoke, so refuse instead (issue #1401).
            return WarmupResult(
                backend=backend_name,
                seconds=time.monotonic() - checked,
                lock_path=lock_path,
                lock_held=False,
                waited_s=waited,
                refused=True,
                detail=(
                    f"lock file {lock_path} is not a safe regular file; "
                    "warm-up refused"
                ),
            )
        purged = purge_cache(roots) if (purge_stale and held) else 0
        started = time.monotonic()
        detail = backend.warm_jit_cache()
        seconds = time.monotonic() - started
        # Stamped inside the lock, so the fingerprint recorded is the one the
        # compile just produced and not one a later writer has already moved.
        if held:
            _write_stamp(stamp_path, cache_fingerprint(roots))
    return WarmupResult(
        backend=backend_name,
        seconds=seconds,
        lock_path=lock_path,
        lock_held=held,
        waited_s=waited,
        detail=detail,
        purged=purged,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def stamp_vouches_for(roots: Sequence[Path]) -> bool:
    """True when the cache on disk is exactly what the last serial warm-up left.

    A persistent per-runner cache makes the warm-up cost 0.3 s instead of a
    25-45 s cold compile per job, but a cache a racing or killed writer
    poisoned would persist too, and the stamp is the only witness that none
    did. The purge itself happens INSIDE the compile lock (``purge_stale``);
    this is the unlocked read for reporting and tests.
    """
    fingerprint = cache_fingerprint(roots)
    return bool(fingerprint) and fingerprint == _read_stamp(warmup_stamp_path())


def purge_cache(roots: Sequence[Path]) -> int:
    """Delete every ``*.nbi``/``*.nbc`` under ``roots`` and the stamp.

    The repair for a cache that is ALREADY corrupt, which the warm-up cannot
    do: loading a corrupt entry is what segfaults, so no in-process code runs
    after it. CI calls this as workspace hygiene so a poisoned cache cannot
    outlive a job.

    Returns the number of artifacts removed. The stamp goes with them, since
    a stamp describing a cache that no longer exists would make the next
    warm-up skip against a fingerprint of nothing.

    A no-op on an empty ``roots``: the lock, fingerprint and stamp are keyed
    on the ENVIRONMENT (interpreter prefix and ``NUMBA_CACHE_DIR``), not on a
    particular backend's roots, so a backend with no JIT cache of its own
    (issue #1572 review) shares that key with every real cache under the
    same venv. Purging nothing must not delete a stamp describing something
    real - that would force a spurious full recompile on the next warm-up
    for a reason that has nothing to do with this call.
    """
    if not roots:
        return 0
    removed = 0
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix in (".nbi", ".nbc") and path.is_file():
                path.unlink()
                removed += 1
    warmup_stamp_path().unlink(missing_ok=True)
    return removed


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m apps.analysis.jit_warmup``; the parser lives in jit_warmup_cli.

    Imported lazily because the CLI module imports this one.
    """
    from .jit_warmup_cli import main as cli_main

    return cli_main(argv)


__all__ = [
    "LOCK_DIR_ENV",
    "LOCK_TIMEOUT_S",
    "WARMUP_LINE_PREFIX",
    "JitWarmable",
    "WarmupResult",
    "cache_fingerprint",
    "main",
    "purge_cache",
    "stamp_vouches_for",
    "toolchain_identity",
    "warm_backend_jit",
    "warmup_lock_path",
    "warmup_stamp_path",
]


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
