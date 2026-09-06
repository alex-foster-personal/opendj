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
the ``*.nbi``/``*.nbc`` artifacts (path, size, mtime) under the backend's
cache roots and comparing against a stamp file written at the end of a
warm-up. The invariant it asserts is exactly the one that matters - "this
cache was produced by a serial compile and nothing has written to it since" -
and anything else, including a purge, a librosa upgrade, or a racing writer,
changes the fingerprint and warms again under the lock.

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
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Protocol


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

    def render(self) -> str:
        if self.skipped:
            return (
                f"{WARMUP_LINE_PREFIX} backend={self.backend} "
                f"seconds={self.seconds:.2f} skipped=1 {self.detail}"
            )
        lock = "locked" if self.lock_held else "UNLOCKED"
        return (
            f"{WARMUP_LINE_PREFIX} backend={self.backend} "
            f"seconds={self.seconds:.2f} lock={lock} "
            f"waited_s={self.waited_s:.2f} {self.detail}"
        )


# ---------------------------------------------------------------------------
# Lock placement
# ---------------------------------------------------------------------------


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


def warmup_stamp_path() -> Path:
    """Where the fingerprint of the last successful serial warm-up is kept.

    Beside the lock, in the system temp directory, for the same reason: the
    processes racing for one cache on a CI host are not always the same UNIX
    user, and a stamp only one of them can read is not a stamp.
    """
    return Path(tempfile.gettempdir()) / f"mdt-numba-warmup-{_env_key()}.stamp"


def cache_fingerprint(roots: Sequence[Path]) -> str:
    """Fingerprint the JIT cache artifacts under ``roots``.

    Empty string when ``roots`` is empty, when no artifact exists, or when a
    root cannot be walked. Every one of those means "I cannot vouch for this
    cache", and the caller treats an empty fingerprint as never matching a
    stamp, so the failure mode is an extra warm-up rather than a skipped one.

    Size and mtime rather than content: the point is to notice that something
    wrote to the cache, and hashing tens of megabytes of object code on every
    CLI start would cost more than the warm-up it is avoiding.
    """
    try:
        artifacts = _cache_artifacts(roots)
    except OSError as exc:
        log.error(
            "jit-warmup: cannot fingerprint cache roots %s (%s); warming "
            "unconditionally",
            [str(r) for r in roots],
            exc,
        )
        return ""
    entries: list[str] = []
    for path in artifacts:
        st = path.stat()
        entries.append(f"{path}\0{st.st_size}\0{st.st_mtime_ns}")
    if not entries:
        return ""
    digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
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


def warmup_lock_path() -> Path:
    """Where the compile lock for THIS environment's numba cache lives.

    The lock has to be shared by every process that writes the same cache and
    by nobody else, so it is keyed on what determines the cache location:
    the interpreter prefix (which venv's ``site-packages`` numba writes beside)
    and ``NUMBA_CACHE_DIR`` (which overrides that when set).

    The file itself goes in the system temp directory rather than beside the
    cache. The cache lives inside ``site-packages``, and the processes racing
    for it on a CI host are not always the same UNIX user - agentbox has run
    the same venv as both ``root`` and ``ghrunner``, which is how root-owned
    bytecode there once broke checkout. A lock nobody but the venv owner can
    open is not a lock. The temp directory is writable by all of them, and the
    key makes the name unambiguous.
    """
    return Path(tempfile.gettempdir()) / f"mdt-numba-warmup-{_env_key()}.lock"


@contextmanager
def _compile_lock(path: Path, timeout_s: float) -> Iterator[tuple[bool, float]]:
    """Hold an exclusive advisory lock on ``path``; yield (held, waited_s).

    Yields ``held=False`` rather than raising when the lock cannot be taken.
    The warm-up is a mitigation, and failing the whole analysis run because a
    lock file could not be opened would turn a slow start into an outage. Every
    such degradation is logged at ERROR with the reason, never swallowed.

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
        yield False, 0.0
        return

    fd: int | None = None
    try:
        # 0o666 so a second UNIX user on the same host can take the same lock;
        # the umask still applies, which is why the mode is repaired below.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            os.fchmod(fd, 0o666)
        except OSError:
            # Not ours to chmod (another user created it). Harmless: we only
            # need to have opened it, which we just did.
            pass
    except OSError as exc:
        log.error(
            "jit-warmup: cannot open lock file %s (%s); warming WITHOUT a "
            "cross-process lock",
            path,
            exc,
        )
        yield False, time.monotonic() - started
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
                    yield False, time.monotonic() - started
                    return
                time.sleep(_LOCK_POLL_S)
        waited = time.monotonic() - started
        try:
            yield True, waited
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------


def warm_backend_jit(
    backend: type[JitWarmable],
    *,
    backend_name: str,
    timeout_s: float = LOCK_TIMEOUT_S,
) -> WarmupResult:
    """Compile ``backend``'s cached JIT paths once, under the compile lock.

    Always runs, for every worker count. ``--workers 1`` is not exempt: the
    corruption needs two PROCESSES, not two workers, and one CLI invocation
    per chunk is exactly two processes when a drain and a refresh overlap.
    """
    # Named, not inherited. A backend missing either method used to die with a
    # bare `AttributeError: type object 'X' has no attribute 'jit_cache_roots'`
    # from inside the warm-up, which reads as a bug in this module rather than
    # as "that backend has not declared whether it has a JIT cache". It is not
    # defaulted, because a silent `()` would make an undeclared backend take
    # the "nothing to protect" path, which is exactly the wrong guess.
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

    with _compile_lock(lock_path, timeout_s) as (held, waited):
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
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def purge_cache(roots: Sequence[Path]) -> int:
    """Delete every ``*.nbi``/``*.nbc`` under ``roots`` and the stamp.

    The repair for a cache that is ALREADY corrupt, which the warm-up cannot
    do: loading a corrupt entry is what segfaults, so no in-process code runs
    after it. CI calls this as workspace hygiene so a poisoned cache cannot
    outlive a job.

    Returns the number of artifacts removed. The stamp goes with them, since
    a stamp describing a cache that no longer exists would make the next
    warm-up skip against a fingerprint of nothing.
    """
    removed = 0
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix in (".nbi", ".nbc") and path.is_file():
                path.unlink()
                removed += 1
    warmup_stamp_path().unlink(missing_ok=True)
    return removed


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m apps.analysis.jit_warmup`` - purge and/or warm, and prove it.

    Exists so CI and a developer run the SAME command the analysis CLI runs
    internally, rather than CI carrying its own inline copy of the warm-up
    that can drift from the one that ships.

    Exits non-zero when a backend that declares cache roots ends the warm-up
    with no artifacts on disk. That is the positive check: a warm-up step
    that silently warmed nothing is exactly the green-but-useless signal this
    whole issue was hidden behind.
    """
    import argparse

    from .backends import DEFAULT_BACKEND, get_backend

    parser = argparse.ArgumentParser(
        prog="apps.analysis.jit_warmup",
        description="Purge and/or warm the analysis backend's numba JIT cache.",
    )
    parser.add_argument("--backend", default=DEFAULT_BACKEND)
    parser.add_argument(
        "--purge",
        action="store_true",
        help="delete existing cache artifacts and the stamp before warming",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    backend = get_backend(args.backend)
    roots = tuple(backend.jit_cache_roots())
    print(f"{WARMUP_LINE_PREFIX} roots={[str(r) for r in roots]}")

    if args.purge:
        removed = purge_cache(roots)
        left = len(_cache_artifacts(roots))
        print(f"{WARMUP_LINE_PREFIX} purged={removed} remaining={left}")
        if left:
            print(
                f"[ERROR] {left} JIT cache artifacts survived the purge; a "
                "corrupt one among them would keep killing every later process",
                file=sys.stderr,
            )
            return 1

    result = warm_backend_jit(backend, backend_name=args.backend)
    print(result.render())

    artifacts = len(_cache_artifacts(roots))
    print(f"{WARMUP_LINE_PREFIX} artifacts={artifacts}")
    if roots and artifacts == 0:
        print(
            f"[ERROR] backend {args.backend!r} declares JIT cache roots but "
            "warmed zero artifacts onto disk; the workers would compile into "
            "a cold shared cache concurrently, which is issue #1316",
            file=sys.stderr,
        )
        return 1
    return 0


__all__ = [
    "JitWarmable",
    "LOCK_TIMEOUT_S",
    "WARMUP_LINE_PREFIX",
    "WarmupResult",
    "cache_fingerprint",
    "main",
    "purge_cache",
    "warm_backend_jit",
    "warmup_lock_path",
    "warmup_stamp_path",
]


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
