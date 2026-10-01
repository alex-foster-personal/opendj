"""Analyser backend protocol + shared exceptions."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..record import AnalysisRecord


class BackendNotAvailable(RuntimeError):
    """Backend runtime dep missing (e.g. ``mixed-in-key-cli`` not on PATH)."""


class BackendNonshippable(RuntimeError):
    """Backend is registered but refused for licensing reasons.

    Distinct from :class:`BackendNotAvailable`: the backend's dependency may
    be perfectly importable, but its weights carry a license (e.g. madmom's
    CC BY-NC-SA pretrained models, NATIVE-08) that forbids shipping it, so
    the registry refuses to resolve it unless the caller opts in explicitly
    with ``MDT_BENCH_NONSHIPPABLE=1``.
    """


class TrackTooLong(RuntimeError):
    """Track exceeded ``analyzer.max_track_minutes``."""


class TrackUnreadable(RuntimeError):
    """THIS FILE could not be read or decoded.

    The one failure a backend can have that is a fact about the input rather
    than about the machine, so it is the one a chunking caller may keep going
    past. A backend raises it where it knows the file is the problem - a
    decode that failed, an empty stream, a converter that rejected it - and
    lets everything else propagate, because a missing config, a broken model
    download or a dead worker pool will meet the next file exactly the same
    way and the caller needs to be told to stop.
    """


class TrackVanished(RuntimeError):
    """THIS FILE was gone by the time the backend opened it.

    Deliberately not a subclass of :class:`TrackUnreadable`, because the
    caller has to do something different with it. An unreadable file is here
    and really was attempted, so recording the queue as tried is honest. A
    file that disappeared between the caller's admission check and the decode
    was never attempted at all, and if it is restored byte-identically its
    content token is unchanged - so a queue booked as tried would leave that
    track sitting behind an `unchanged` verdict forever, never analyzed.

    The window is small but real: the drain checks ``exists()`` when it
    builds the handoff and the backend opens the file some chunks later,
    with a library sync, a rename or an unmount free to happen in between.
    """


@runtime_checkable
class AnalyzerBackend(Protocol):
    name: str
    version: str

    @classmethod
    def analyze(cls, path: Path, stable_id: str) -> AnalysisRecord:
        """Pure compute; must not write to disk or state layer."""
        ...  # pragma: no cover

    @classmethod
    def jit_cache_roots(cls) -> tuple[Path, ...]:
        """Directories whose ``*.nbi``/``*.nbc`` artifacts this backend writes.

        Returned so the caller can fingerprint the on-disk cache and tell a
        cache this process already warmed from one a concurrent writer has
        touched since. An empty tuple means "this backend writes no JIT cache
        and I cannot vouch for one", which makes the caller warm every time
        rather than trust a fingerprint it has no way to compute.

        Must not import the heavy analysis stack: it is called on the fast
        path where the whole point is not paying that import.
        """
        ...  # pragma: no cover

    @classmethod
    def warm_jit_cache(cls) -> str:
        """Compile every cached JIT path this backend uses, and say what.

        Called ONCE in the parent, serially, under a cross-process lock,
        before any concurrency exists (:mod:`apps.analysis.jit_warmup`). A
        backend whose hot paths are numba ``cache=True`` functions MUST drive
        them here on a synthetic signal, because two processes compiling into
        one on-disk cache corrupt it and every later reader of that cache dies
        at a NULL instruction pointer with no traceback (issue #1316).

        Returns a short human-readable description of what was warmed, which
        the CLI prints. A backend with no JIT cache says so rather than
        staying silent, so an empty warm-up is a stated fact and not an
        unnoticed no-op.

        This is the one place a backend may not be lazy about: it must call
        the same entry points ``analyze`` calls, with the same dtypes, since
        numba caches per type signature.
        """
        ...  # pragma: no cover


def librosa_numba_cache_roots() -> tuple[Path, ...]:
    """Where numba writes librosa's cached compilations, for any backend using it.

    ``NUMBA_CACHE_DIR`` wins when set, because numba then puts every artifact
    there instead of beside the source. Otherwise the artifacts land in
    ``__pycache__`` directories inside the installed ``librosa`` package, so
    the package directory is the root to walk.

    Located with ``find_spec`` rather than ``import librosa``: this runs on the
    fast path, where skipping the librosa import is most of the saving. Returns
    ``()`` when librosa is not installed, which makes the caller warm
    unconditionally and get the backend's honest ``BackendNotAvailable`` from
    its ``warm_jit_cache`` instead of a silent skip.
    """
    override = os.environ.get("NUMBA_CACHE_DIR")
    if override:
        return (Path(override),)
    try:
        spec = importlib.util.find_spec("librosa")
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return ()
    if spec is None or not spec.origin:
        return ()
    return (Path(spec.origin).parent,)


__all__ = [
    "AnalyzerBackend",
    "BackendNotAvailable",
    "TrackTooLong",
    "TrackUnreadable",
    "TrackVanished",
    "librosa_numba_cache_roots",
]
