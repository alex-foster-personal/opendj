"""Command line for the numba JIT warm-up: ``python -m apps.analysis.jit_warmup``.

Split out of :mod:`apps.analysis.jit_warmup` so that module stays the library
(lock, fingerprint, stamp, warm) and this one owns argument parsing and the
printed contract. ``jit_warmup.main`` forwards here, so the pinned invocation
in every workflow is unchanged.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from . import backends
from .jit_warmup import (
    WARMUP_LINE_PREFIX,
    _cache_artifacts,
    purge_cache,
    toolchain_identity,
    warm_backend_jit,
)


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
    parser = argparse.ArgumentParser(
        prog="apps.analysis.jit_warmup",
        description="Purge and/or warm the analysis backend's numba JIT cache.",
    )
    parser.add_argument("--backend", default=backends.DEFAULT_BACKEND)
    parser.add_argument(
        "--purge",
        action="store_true",
        help="delete existing cache artifacts and the stamp before warming",
    )
    parser.add_argument(
        "--identity",
        action="store_true",
        help="print the toolchain identity the cache is keyed on and exit",
    )
    parser.add_argument(
        "--purge-if-stale",
        action="store_true",
        help=(
            "purge only when the stamp does not vouch for the cache on disk; a "
            "cache the last serial warm-up left untouched is kept and skipped"
        ),
    )
    args = parser.parse_args(argv)
    if args.identity:
        print(toolchain_identity())
        return 0

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Resolved through the module, not a bound name, so a registry patched
    # after import (the cold-cache test does this) is honored.
    backend = backends.get_backend(args.backend)
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

    # A stale check that purged here, before the lock, could delete what a
    # concurrent lock holder is writing; the check and purge happen inside.
    result = warm_backend_jit(
        backend, backend_name=args.backend, purge_stale=args.purge_if_stale
    )
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
