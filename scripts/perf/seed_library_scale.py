"""Seed a disposable library data-dir with on-disk audio for S12 perf capture.

Writes ``<out>/state/state.db`` and one resolvable mp3 per track under
``<out>/audio/`` so ``GET /api/v1/library/readiness`` uses denominator
``present`` equal to ``--count``. Complements
``scripts.perf.generate_library_scale_fixture``, which shares one WAV and is
not S12 release evidence; this seed is for CloudSync timing with honest
present counts (issue #11265, parent #2899).

Output must be a new or empty directory, or prior output carrying this
helper's valid ownership marker. Unowned nonempty directories are refused.

Usage::

    uv run python -m scripts.perf.seed_library_scale --count 10000 --out /tmp/s12-10k
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter

DEFAULT_COUNT: int = 10_000
MAX_COUNT: int = 100_000
DURATION_MS: int = 180_000
ACTOR: str = "perf-seed-library-scale"
SEED_MARKER_NAME: str = "library-scale-seed-owner.txt"
SEED_MARKER_CONTENT: str = "seed_library_scale:v1\n"
# Same tiny on-disk mp3 stub as tests/webui/test_listing_query_count (ID3 header + body).
_MINIMAL_MP3: bytes = b"ID3" + b"\x00" * 128 + bytes(1024)


@dataclass(frozen=True)
class SeedSpec:
    count: int
    out: Path


def _validate_spec(spec: SeedSpec) -> None:
    if not spec.out.is_absolute():
        raise ValueError(f"--out must be absolute, got {spec.out}")
    if not 1 <= spec.count <= MAX_COUNT:
        raise ValueError(f"--count must be between 1 and {MAX_COUNT}")


def _stable_id(index: int) -> str:
    return f"{index:040x}"


def _reset_seed_dirs(out: Path) -> None:
    """Reset only new, empty, or marked seed output; preserve other root files."""
    if out.is_symlink():
        raise ValueError(f"refusing symlink --out: {out}")
    marker = out / SEED_MARKER_NAME
    if out.exists():
        if not out.is_dir():
            raise ValueError(f"--out is not a directory: {out}")
        if any(out.iterdir()) and (
            marker.is_symlink() or not marker.is_file() or marker.read_bytes() != SEED_MARKER_CONTENT.encode("utf-8")
        ):
            raise ValueError(
                f"refusing to reset {out}: missing or invalid {SEED_MARKER_NAME}; choose a new or empty directory"
            )
    out.mkdir(parents=True, exist_ok=True)
    for name in ("state", "audio"):
        path = out / name
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    marker.write_text(SEED_MARKER_CONTENT, encoding="utf-8", newline="\n")


def _write_audio_files(audio_dir: Path, count: int) -> list[Path]:
    audio_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index in range(count):
        path = audio_dir / f"track-{index:05d}.mp3"
        path.write_bytes(_MINIMAL_MP3)
        paths.append(path)
    return paths


def seed(out: Path, *, count: int) -> None:
    """Build tracks and mp3 files in a new, empty, or owned seed directory."""
    spec = SeedSpec(count=count, out=out)
    _validate_spec(spec)
    _reset_seed_dirs(out)
    audio_paths = _write_audio_files(out / "audio", count)
    state_path = out / "state" / "state.db"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    connection = state_db.open_rw(state_path)
    try:
        bus = FakeEventBus()
        with StateWriter(connection, bus, actor=ACTOR) as writer:
            for index, audio_path in enumerate(audio_paths):
                writer.upsert_track(
                    stable_id=_stable_id(index),
                    stable_id_tier="inferred",
                    title=f"S12 scale track {index:05d}",
                    artists=[f"Artist {index % 1000:04d}"],
                    album=None,
                    isrc=None,
                    duration_ms=DURATION_MS,
                    file_path=str(audio_path.resolve()),
                )
    finally:
        connection.close()
    _verify(out, count)


def _verify(out: Path, count: int) -> None:
    state_path = out / "state" / "state.db"
    connection = sqlite3.connect(str(state_path))
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL",
        ).fetchone()
        tracks = 0 if row is None else int(row[0])
    finally:
        connection.close()
    if tracks != count:
        raise RuntimeError(f"expected {count} tracks in state.db, found {tracks}")
    audio_dir = out / "audio"
    on_disk = sum(1 for p in audio_dir.glob("track-*.mp3") if p.is_file())
    if on_disk != count:
        raise RuntimeError(f"expected {count} mp3 files under audio/, found {on_disk}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="absolute MDT_DATA_DIR root: new, empty, or owned by this seed helper",
    )
    args = parser.parse_args(argv)
    started = time.perf_counter()
    try:
        seed(args.out, count=args.count)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    elapsed = time.perf_counter() - started
    print(
        f"[OK] seed_library_scale count={args.count} out={args.out} elapsed_s={elapsed:.2f}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
