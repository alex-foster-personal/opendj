"""``python -m apps.dedup.scan`` -- fingerprint the music library.

Walks the configured roots (``paths.MUSIC_ROOTS`` by default), computes a
chromaprint fingerprint for every audio file, and persists rows to the
``fingerprints`` cache table (see :class:`apps.shared.fingerprints.FingerprintCache`).

Incremental on re-run: files whose ``(size, mtime)`` match the cache row
are skipped. ``--force-recompute`` wipes and regenerates.

Never raises on individual-file errors; instead records them in a summary
counter and prints at the end. A missing fingerprint backend (no engine
build and no ``fpcalc``) is the exception -- we raise early with the
remediation from :class:`ChromaprintMissing` since no file can be
fingerprinted without it.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from apps.shared import audio_files, paths
from apps.shared.fingerprints import (
    ChromaprintMissing,
    FingerprintCache,
    compute,
)
from apps.shared.stable_id import stable_id


@dataclass(slots=True)
class ScanResult:
    """Outcome of a ``scan`` run."""

    scanned: int = 0
    computed: int = 0
    cache_hits: int = 0
    errors: int = 0
    chromaprint_missing: bool = False

    def summary(self) -> str:
        return (
            f"scanned={self.scanned} computed={self.computed} "
            f"cache_hits={self.cache_hits} errors={self.errors} "
            f"chromaprint_missing={self.chromaprint_missing}"
        )


def run_scan(
    *,
    roots: list[Path] | None = None,
    db_path: Path | None = None,
    force_recompute: bool = False,
    on_progress=None,
) -> ScanResult:
    """Programmatic entrypoint; called by both the CLI and tests.

    Parameters
    ----------
    roots
        Music tree roots to scan. Defaults to ``paths.MUSIC_ROOTS``.
    db_path
        Cache database location. Defaults to ``paths.DEDUP_FALLBACK_DB``.
    force_recompute
        If True, recompute every file even when the cache hit would hit.
    on_progress
        Optional callable ``(af: AudioFile, result: str) -> None`` that
        tests (or a rich progress bar) can plug in.
    """
    use_roots = roots if roots is not None else paths.MUSIC_ROOTS
    use_db = db_path if db_path is not None else paths.DEDUP_FALLBACK_DB
    cache = FingerprintCache(use_db)

    out = ScanResult()
    try:
        for af in audio_files.scan_music_files(use_roots):
            out.scanned += 1
            status = "hit"
            try:
                if force_recompute:
                    fp = compute(af.path)
                    cache.put(
                        fp,
                        stable_id=stable_id(
                            isrc=None,
                            fingerprint=fp.fp_str,
                            duration_ms=int(fp.duration * 1000),
                            size_bytes=fp.size,
                        )[0],
                    )
                    out.computed += 1
                    status = "recomputed"
                else:
                    existing = cache.get(af.path)
                    if existing is not None:
                        out.cache_hits += 1
                        status = "hit"
                    else:
                        fp = compute(af.path)
                        cache.put(
                            fp,
                            stable_id=stable_id(
                                isrc=None,
                                fingerprint=fp.fp_str,
                                duration_ms=int(fp.duration * 1000),
                                size_bytes=fp.size,
                            )[0],
                        )
                        out.computed += 1
                        status = "computed"
            except ChromaprintMissing:
                # Bubble up: the whole scan cannot proceed without fpcalc.
                out.chromaprint_missing = True
                raise
            except Exception as exc:  # noqa: BLE001 -- degrade gracefully
                out.errors += 1
                status = f"error: {type(exc).__name__}"
            if on_progress is not None:
                on_progress(af, status)
    except ChromaprintMissing:
        # Fall through; main handles the user message.
        raise

    return out


# ---------------------------------------------------------------- CLI


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.dedup.scan",
        description="Fingerprint the music library (Phase 7 dedup).",
    )
    p.add_argument(
        "--root",
        action="append",
        type=Path,
        help="Music tree root; repeat for multiple. "
        "Defaults to paths.MUSIC_ROOTS.",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite cache DB. Defaults to data/dedup/phase7.sqlite.",
    )
    p.add_argument(
        "--force-recompute",
        action="store_true",
        help="Recompute every fingerprint even when the cache matches.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-file status lines.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        from rich.console import Console

        console = Console(quiet=args.quiet)
    except Exception:  # pragma: no cover -- rich always installed
        console = None

    def on_progress(af, status):
        if args.quiet or console is None:
            return
        console.print(f"  {status:>10s}  {af.path}")

    try:
        out = run_scan(
            roots=args.root,
            db_path=args.db,
            force_recompute=args.force_recompute,
            on_progress=on_progress,
        )
    except ChromaprintMissing as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "Build the engine (`cargo build --release --manifest-path "
            "apps/audio-engine/Cargo.toml`), or install chromaprint's fpcalc, "
            "then retry.",
            file=sys.stderr,
        )
        return 2

    if console is not None and not args.quiet:
        console.rule("dedup scan summary")
        console.print(out.summary())
    else:
        print(out.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
