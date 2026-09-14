"""Throwaway library with one real FLAC demucs4 stem bundle for stem-decode bench.

Builds one ingested track plus a schema v2 bundle under ``data/state/stems/``.
No mocked APIs: audio is synthesised, FLAC parts are written by ffmpeg, and the
track row comes from the real folder ingest.

Usage::

    uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.stem_decode_fixture \\
        --data-dir /abs/path/to/stem-decode-data [--manifest /abs/manifest.json]

Acceptance tests (tests/scripts/test_stem_decode_fixture.py):
  [if] --data-dir is relative [then] exit non-zero before writing
  [if] ffmpeg is missing [then] exit 3 UNKNOWN
  [if] ingest writes zero tracks [then] exit non-zero
  [if] manifest is requested [then] it names the stable_id and bundle layout
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from apps.webui.frontend.tests.e2e.support.deckload_fixture import (
    FIXTURE_REVISION,
    FixtureTrack,
    _ingest_and_verify,
    _track_rows,
    ensure_audio,
)
from apps.stems.artifacts import STEM_PARTS

FIXTURE_REVISION_STEM: str = f"{FIXTURE_REVISION}-stem-decode-bench-v1"
BENCH_TRACK = FixtureTrack(
    filename="stem-decode-bench-track.wav",
    bpm=128.0,
    seconds=12.0,
)
SAMPLE_RATE_HZ = 44_100


@dataclass(frozen=True)
class StemDecodeFixture:
    stable_id: str
    layout: str
    revision: str


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        print("UNKNOWN: ffmpeg is required to encode FLAC stem parts", file=sys.stderr)
        raise SystemExit(3)


def _write_flac_part(path: Path, seconds: float, *, frequency_hz: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency_hz}:duration={seconds}:sample_rate={SAMPLE_RATE_HZ}",
            "-ac",
            "2",
            str(path),
        ],
        check=True,
    )


def _write_v2_bundle(stems_root: Path, stable_id: str, seconds: float) -> None:
    bundle_dir = stems_root / stable_id
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)
    files: dict[str, str] = {}
    freqs = {"vocals": 880.0, "drums": 220.0, "bass": 110.0, "other": 440.0}
    for part in STEM_PARTS:
        filename = f"{part}.flac"
        _write_flac_part(bundle_dir / filename, seconds, frequency_hz=freqs[part])
        files[part] = filename
    frame_count = round(seconds * SAMPLE_RATE_HZ)
    manifest = {
        "schema_version": 2,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {
            "path": str(bundle_dir / "source.wav"),
            "sha256": hashlib.sha256(stable_id.encode()).hexdigest(),
        },
        "files": files,
        "audio": {
            "sample_rate": SAMPLE_RATE_HZ,
            "frame_count": frame_count,
            "channels": 2,
        },
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def build(data_dir: Path) -> StemDecodeFixture:
    _require_ffmpeg()
    data_dir = data_dir.resolve()
    audio_dir = data_dir / "audio"
    marker = data_dir / ".stem-decode-fixture-revision"
    state_db = data_dir / "state" / "state.db"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == FIXTURE_REVISION_STEM:
        rows = _track_rows(state_db)
        if len(rows) == 1:
            stable_id = rows[0][0]
            bundle = data_dir / "state" / "stems" / stable_id / "manifest.json"
            if bundle.is_file():
                return StemDecodeFixture(
                    stable_id=stable_id,
                    layout="demucs4",
                    revision=FIXTURE_REVISION_STEM,
                )
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    files = ensure_audio(audio_dir, (BENCH_TRACK,))
    rows = _ingest_and_verify(data_dir, audio_dir, files, "stem-decode-bench")
    stable_id = rows[0][0]
    _write_v2_bundle(data_dir / "state" / "stems", stable_id, BENCH_TRACK.seconds)
    marker.write_text(f"{FIXTURE_REVISION_STEM}\n", encoding="utf-8")
    return StemDecodeFixture(
        stable_id=stable_id,
        layout="demucs4",
        revision=FIXTURE_REVISION_STEM,
    )


def write_manifest(path: Path, fixture: StemDecodeFixture) -> None:
    payload = {
        "revision": fixture.revision,
        "stable_id": fixture.stable_id,
        "layout": fixture.layout,
        "denominator": (
            "one demucs4 FLAC bundle (four parts, 12.0 s stereo 44.1 kHz each) "
            "decoded through the production browser stem path after a mix load"
        ),
        "tracks": [{"stable_id": fixture.stable_id, "title": BENCH_TRACK.filename}],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(payload, indent=2)}\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.data_dir.is_absolute():
        print(f"[ERROR] --data-dir must be absolute, got {args.data_dir!r}", file=sys.stderr)
        return 2
    if args.manifest is not None and not args.manifest.is_absolute():
        print(f"[ERROR] --manifest must be absolute, got {args.manifest!r}", file=sys.stderr)
        return 2
    fixture = build(args.data_dir)
    print(
        f"[stem-decode-fixture] revision={fixture.revision} stable_id={fixture.stable_id} "
        f"layout={fixture.layout} data_dir={args.data_dir}"
    )
    if args.manifest is not None:
        write_manifest(args.manifest, fixture)
        print(f"[stem-decode-fixture] manifest={args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
