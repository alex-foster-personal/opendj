#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = ["musdb==0.4.0", "soundfile>=0.12", "numpy<2"]
# ///
"""Fixture puller for the local-stems research spike (issue #1461).

Exports N real tracks (mixture + TRUE vocals/drums/bass/other stems) from the
public **MUSDB18 7-second sample** to plain WAV files, so the candidate
scripts and the SI-SDR scorer never need musdb/stempeg installed.

WHY THIS SET AND NOT FULL MUSDB18-HQ. apps/stems/CLAUDE.md's own rule is
"download only the smallest subset that answers the question", and the full
HQ corpus (~22.7 GB) needs a Zenodo access request this box does not have and
would not fit this spike's time budget anyway. The 7-second sample is the
`musdb` package's own public, no-auth, ~140 MB test fixture -- 144 real
7-second clips (some cropped from MUSDB18 train, some from test) with REAL
ground-truth stems. It is downloaded fresh into a gitignored cache dir, never
committed, matching the corpora rule in apps/stems/CLAUDE.md.

HONESTY ABOUT CLIP LENGTH. Each clip is ~6.8s of audio (300032 samples at
44100 Hz), not a full 3-4 minute track. Every number this spike reports from
these clips is a clip-length number, labelled as such -- see
docs/research/local-stems-*.md. There is no full-length, rights-clear audio
file reachable from the benchmark host (it has no access to a user library or
to a licensed asset store), so no full-track wall-clock claim is
made here; that gap is named explicitly rather than papered over with a
synthesized or concatenated stand-in.

Run:
  uv run scripts/bench/local/fixtures.py --out .tmp/local-stems-bench/fixtures --n 2

-Claude
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

STEM_PARTS: tuple[str, ...] = ("vocals", "drums", "bass", "other")
SOURCE_VERSION = "musdb==0.4.0; MUSDB18 7-second sample; local-stems-fixture-manifest-v1"
# This is deliberately the two-clip ranking fixture, not a rolling sample.  Both
# source and exported WAV checksums are trust anchors: a changed resolver payload
# must fail before it can be recorded as a new baseline.
EXPECTED_TRACKS: tuple[dict[str, object], ...] = (
    {
        "name": "A Classic Education - NightOwl",
        "source_sha256": "4ca541371b2d07d65f81ddda3d82cd57b49c403a5480b9e508cda239568b8a55",
        "sha256": {
            "bass.wav": "940594130c61e8e7b581d7fc26bf4b8f8a5868a3036794edabc66b3399b4095e",
            "drums.wav": "018b5245145eabc3e6bbff5a7453ce2d4824a72772e8010f8ea6aa0779346871",
            "mixture.wav": "3e84df9fae920f1f2553656eef409d37465b1ac67ed78270f2fd9d117ae8363e",
            "other.wav": "81ede632b7639d43d521d9a2878ebb86309b919b5bd99da59735593abb19d432",
            "vocals.wav": "c2023440d88a7506aca24e8e744c78d2d452ec02afba03694e7a8369f9b8dd40",
        },
    },
    {
        "name": "ANiMAL - Clinic A",
        "source_sha256": "d22a6e93ede523c154b67460179029329f5913759ed7ad66779ed6f08af97f69",
        "sha256": {
            "bass.wav": "c2310b94e0db0dd2c6960066177d8076a43c394e82b7b76d3db3fa0d4ac0af6f",
            "drums.wav": "390d8656de0192defdfab3c4cb1daa2bb80cc5b6da31ca7969b3a0c2d3d07abc",
            "mixture.wav": "12807b6e668f5e2a4fbf545074b5753c51f3585fec2761078b200c0170b25421",
            "other.wav": "146d5fc0cca739187f8affd8725bbdfcee60fd592e6fadeea66d8c0646fcb04f",
            "vocals.wav": "e881cba342b0acaf74b05298976c199f7143a285acfa80344e46f7436dcefd16",
        },
    },
)


def _write_track(track, expected: dict[str, object], out_dir: Path) -> dict:
    import numpy as np
    import soundfile as sf

    if track.name != expected["name"]:
        raise RuntimeError(f"fixture ordering changed: expected {expected['name']!r}, got {track.name!r}")
    source_path = Path(track.path)
    if _sha256(source_path) != expected["source_sha256"]:
        raise RuntimeError(
            f"fixture source checksum mismatch for {track.name!r}: {source_path}; "
            "refusing to establish fresh truth"
        )
    track_dir = out_dir / track.name.replace("/", "_").replace(" ", "_")
    track_dir.mkdir(parents=True, exist_ok=True)
    mixture = track.audio.astype(np.float32)
    sf.write(str(track_dir / "mixture.wav"), mixture, track.rate, subtype="PCM_16")
    for part in STEM_PARTS:
        stem_audio = track.targets[part].audio.astype(np.float32)
        sf.write(str(track_dir / f"{part}.wav"), stem_audio, track.rate, subtype="PCM_16")
    duration_s = mixture.shape[0] / track.rate
    files = {path.name: _sha256(path) for path in sorted(track_dir.glob("*.wav"))}
    if files != expected["sha256"]:
        raise RuntimeError(
            f"fixture WAV checksum mismatch for {track.name!r}; "
            "refusing to establish fresh truth"
        )
    return {
        "name": track.name,
        "dir": str(track_dir),
        "rate": track.rate,
        "duration_s": round(duration_s, 3),
        "frames": int(mixture.shape[0]),
        "sha256": files,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="output cache dir")
    parser.add_argument("--musdb-root", type=Path, default=None, help="musdb download root")
    parser.add_argument("--n", type=int, default=2, help="how many pinned tracks to export")
    args = parser.parse_args()

    if args.n < 1 or args.n > len(EXPECTED_TRACKS):
        raise ValueError(f"--n must be between 1 and {len(EXPECTED_TRACKS)}, got {args.n}")

    import musdb  # type: ignore[import-not-found]  # musdb ships no type stubs.

    musdb_root = args.musdb_root or (args.out.parent / "musdb18-7s-sample")
    db = musdb.DB(root=str(musdb_root), download=True)
    if len(db) < args.n:
        raise RuntimeError(f"musdb sample has {len(db)} tracks, need {args.n}")

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = [_write_track(track, EXPECTED_TRACKS[index], args.out) for index, track in enumerate(db[: args.n])]
    manifest_path = args.out / "manifest.json"
    manifest_path.write_text(json.dumps({
        "tracks": manifest,
        "source": "musdb18-7s-sample",
        "source_version": SOURCE_VERSION,
    }, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
