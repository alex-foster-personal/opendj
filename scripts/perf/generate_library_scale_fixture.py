"""Build deterministic synthetic libraries for the LIBM-120 scale bench.

Mini-PRD
--------
Requirements:
- [ ] [if] ``--tracks 20000`` is used [then] the fixture contains exactly
  20,000 live tracks and one 20,000-member playlist.
- [ ] [if] the same seed and track count are used twice [then] the generated
  stable ids, titles, artists, and playlist order are identical.
- [ ] [if] the command runs again on a directory it already built [then] it
  rebuilds from an empty directory, so the second database has the same row
  count in every table and the same content as the first, never appended
  memberships, changelog rows or events.
- [ ] [if] ``--data-dir`` is a non-empty directory without this script's
  marker file [then] the command refuses and deletes nothing.
- [ ] [if] ``--data-dir`` is relative [then] the command exits before writing
  anything.
- [ ] [if] the manifest is read [then] it names the seed, track count,
  denominator, generator revision, and the SHA-256 of the shared WAV used for
  the run.
- [ ] [if] the manifest is read [then] it states ``release_evidence: false``:
  numbers measured on this synthetic fixture are a reproducible load floor,
  never LIBM-120 feature or release evidence (AGENTS.md "No mocks and locked
  real fixtures").
- [ ] [if] a fixture is generated [then] its shared audio is a deterministic
  30-second tone/noise WAV with non-zero PCM frames.

The fixture contains no real-library paths or audio. It uses one deterministic
30-second shared WAV path for the real engine's file metadata, while the track
rows and playlist membership are written through ``StateWriter``. Generated
data is throwaway and must not be committed.

Every one of the N rows points at the same WAV and carries generated metadata,
so the fixture cannot reveal costs that come from metadata diversity, file
diversity, media formats or analysis artifacts. It measures how the production
paths scale with row count, and nothing else. A run against a checksum-verified
sanitized database snapshot with a real media corpus of the same size is the
admissible evidence, and no such 20k corpus exists yet.

Usage::

    uv run --no-sync python -m scripts.perf.generate_library_scale_fixture \
        --data-dir /tmp/libm120-20k --tracks 20000 --seed 2328
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sqlite3
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from random import Random

from apps.shared.hashing import sha256_file_hex
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter

FIXTURE_REVISION: int = 3
MANIFEST_NAME: str = "library-scale-fixture.json"
MARKER_NAME: str = "library-scale-fixture-revision.txt"
AUDIO_NAME: str = "shared-fixture.wav"
DEFAULT_TRACKS: int = 20_000
DEFAULT_SEED: int = 2_328
MAX_TRACKS: int = 100_000
AUDIO_SECONDS: int = 30
AUDIO_SAMPLE_RATE_HZ: int = 44_100
AUDIO_TONE_HZ: float = 440.0
AUDIO_NOISE_AMPLITUDE: float = 0.04


@dataclass(frozen=True)
class FixtureSpec:
    """Inputs that define one reproducible fixture."""

    tracks: int
    seed: int


def _validate_spec(spec: FixtureSpec) -> None:
    if not 1 <= spec.tracks <= MAX_TRACKS:
        raise ValueError(f"--tracks must be between 1 and {MAX_TRACKS}")


def _reset_fixture_dir(data_dir: Path) -> None:
    """Empty a fixture directory this script made, so every build starts fresh.

    Every build rebuilds from nothing. Keeping a matching fixture and seeding
    it again is what PR #4547's review caught: ``set_playlist_memberships``
    replaced every membership under new item ids and appended changelog rows
    and events, so each rerun benched a mutated, growing database. A kept
    database could also carry whatever the previous bench's engine wrote into
    it. A rebuild costs seconds, which is cheaper than proving a kept one clean.

    A directory is cleared only when it is empty or carries this script's
    marker file, so a mistyped ``--data-dir`` (a real library's data dir, say)
    is refused instead of emptied.
    """
    marker = data_dir / MARKER_NAME
    if data_dir.exists():
        if not data_dir.is_dir():
            raise ValueError(f"--data-dir is not a directory: {data_dir}")
        children = sorted(data_dir.iterdir())
        if children and not marker.is_file():
            raise ValueError(
                f"refusing to reset {data_dir}: it is not empty and has no {MARKER_NAME}, "
                "so this script did not make it; pass an empty or new directory"
            )
        for child in children:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    data_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{FIXTURE_REVISION}\n", encoding="utf-8")


def _write_audio(path: Path, *, seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    expected_frames = AUDIO_SECONDS * AUDIO_SAMPLE_RATE_HZ
    random = Random(seed)
    tone_step = 2.0 * math.pi * AUDIO_TONE_HZ / AUDIO_SAMPLE_RATE_HZ
    pcm = bytearray()
    for sample_index in range(expected_frames):
        tone = 0.45 * math.sin(tone_step * sample_index)
        noise = random.uniform(-AUDIO_NOISE_AMPLITUDE, AUDIO_NOISE_AMPLITUDE)
        sample = int(max(-1.0, min(1.0, tone + noise)) * 32767)
        pcm.extend(sample.to_bytes(2, byteorder="little", signed=True))
    with wave.open(os.fspath(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(AUDIO_SAMPLE_RATE_HZ)
        handle.writeframes(bytes(pcm))


def _seed_state(data_dir: Path, spec: FixtureSpec, audio_path: Path) -> None:
    state_path = data_dir / "state" / "state.db"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    random = Random(spec.seed)
    playlist_id = f"libm120-scale-{spec.tracks}"
    stable_ids: list[str] = []
    connection = state_db.open_rw(state_path)
    try:
        with StateWriter(connection, actor="libm120-scale-fixture") as writer:
            for index in range(spec.tracks):
                stable_id = f"libm120-track-{index:05d}"
                stable_ids.append(stable_id)
                writer.upsert_track(
                    stable_id=stable_id,
                    stable_id_tier="inferred",
                    title=f"Synthetic Track {index:05d}",
                    artists=[f"Synthetic Artist {random.randrange(1000):04d}"],
                    album=f"Synthetic Album {index // 100:04d}",
                    isrc=None,
                    duration_ms=AUDIO_SECONDS * 1_000,
                    file_path=os.fspath(audio_path),
                )
            writer.insert_playlist(
                playlist_id=playlist_id,
                name=f"LIBM-120 synthetic {spec.tracks}",
                vendor="fixture",
                vendor_pl_id=playlist_id,
            )
            writer.set_playlist_memberships(playlist_id, stable_ids)
    finally:
        connection.close()


def _write_manifest(data_dir: Path, spec: FixtureSpec, audio_path: Path) -> None:
    manifest = {
        "audio_sha256": sha256_file_hex(audio_path),
        "generator_revision": FIXTURE_REVISION,
        "release_evidence": False,
        "release_evidence_reason": (
            "synthetic rows sharing one generated WAV; a load floor, not LIBM-120 feature or release evidence"
        ),
        "seed": spec.seed,
        "tracks_requested": spec.tracks,
        "track_denominator": "present",
        "playlist_id": f"libm120-scale-{spec.tracks}",
        "synthetic": True,
    }
    (data_dir / MANIFEST_NAME).write_text(f"{json.dumps(manifest, indent=2, sort_keys=True)}\n", encoding="utf-8")


def _verify(data_dir: Path, spec: FixtureSpec) -> None:
    state_path = data_dir / "state" / "state.db"
    connection = state_db.open_ro(state_path)
    try:
        tracks = connection.execute("SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL").fetchone()[0]
        members = connection.execute(
            "SELECT COUNT(*) FROM playlist_memberships WHERE playlist_id = ?",
            (f"libm120-scale-{spec.tracks}",),
        ).fetchone()[0]
    finally:
        connection.close()
    if tracks != spec.tracks or members != spec.tracks:
        raise RuntimeError(f"fixture verification failed: tracks={tracks}, members={members}, expected={spec.tracks}")


def build(data_dir: Path, *, tracks: int = DEFAULT_TRACKS, seed: int = DEFAULT_SEED) -> None:
    """Build and verify one deterministic synthetic scale fixture."""
    if not data_dir.is_absolute():
        raise ValueError("--data-dir must be absolute")
    spec = FixtureSpec(tracks=tracks, seed=seed)
    _validate_spec(spec)
    _reset_fixture_dir(data_dir)
    audio_path = data_dir / "audio" / AUDIO_NAME
    _write_audio(audio_path, seed=spec.seed)
    _seed_state(data_dir, spec, audio_path.resolve())
    _verify(data_dir, spec)
    _write_manifest(data_dir, spec, audio_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--tracks", type=int, default=DEFAULT_TRACKS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    try:
        build(args.data_dir, tracks=args.tracks, seed=args.seed)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    print(
        f"[OK] LIBM-120 fixture: tracks={args.tracks} seed={args.seed} data_dir={args.data_dir}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
