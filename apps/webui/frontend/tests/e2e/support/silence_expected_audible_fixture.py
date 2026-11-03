"""Throwaway library for silence-expected-audible e2e (AUDIOLIVE-09, #6636).

Serves the committed bench clips and silent-intro fixture through the REAL
FastAPI routes (/tracks, /audio, /stems, /anlz, /hot-cues). No browser-side
fetch substitution.

Usage::

    uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.silence_expected_audible_fixture \\
        --data-dir /abs/path/to/silence-expected-audible-data [--manifest /abs/manifest.json]
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

from apps.shared.disposable_dirs import refuse_protected_target
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.stems.artifacts import ROFORMER_PARTS
from apps.webui.frontend.tests.e2e.support.deckload_fixture import REPOSITORY_ROOT, _state_cli

FIXTURE_REVISION = "silence-expected-audible-v3"
REVISION_MARKER = ".silence-expected-audible-revision"

MIXTURE_STABLE_ID = "a" * 40
SILENT_INTRO_STABLE_ID = "b" * 40

MANIFEST_PATH = (
    REPOSITORY_ROOT
    / "apps/webui/frontend/tests/e2e/fixtures/silence-expected-audible.manifest.json"
)

SOURCE_PATHS: dict[str, str] = {
    "mixture": "(private path)",
    "vocals": "(private path)",
    "instrumental": "(private path)",
    "silentIntro": "tests/fixtures/phase7-dedup/other-silent-intro.mp3",
}


@dataclass(frozen=True)
class SilenceExpectedAudibleFixture:
    mixture_stable_id: str
    silent_intro_stable_id: str
    revision: str


def _verified_repo_file(rel: str) -> tuple[Path, bytes]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entry = manifest["files"].get(rel)
    if entry is None:
        raise SystemExit(f"[ERROR] {rel} missing from {MANIFEST_PATH}")
    abs_path = REPOSITORY_ROOT / rel
    if not abs_path.is_file():
        raise SystemExit(f"[ERROR] locked fixture missing: {abs_path}")
    data = abs_path.read_bytes()
    if len(data) != int(entry["bytes"]):
        raise SystemExit(
            f"[ERROR] {rel} is {len(data)} bytes, manifest says {entry['bytes']}"
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != entry["sha256"]:
        raise SystemExit(f"[ERROR] {rel} digest mismatch: expected {entry['sha256']}, got {digest}")
    return abs_path, data


def _ffprobe_audio(path: Path) -> tuple[int, int, int]:
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(probe.stdout)
    stream = payload["streams"][0]
    duration_sec = float(payload["format"]["duration"])
    sample_rate = int(stream["sample_rate"])
    channels = int(stream["channels"])
    frame_count = round(duration_sec * sample_rate)
    return sample_rate, channels, frame_count


def _transcode_wav_aligned(
    source: Path, dest: Path, *, sample_rate: int, duration_sec: float
) -> None:
    """Match the mix track duration so browser decode aligns with the loaded mix."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ar",
            str(sample_rate),
            "-ac",
            "2",
            "-t",
            f"{duration_sec:.6f}",
            str(dest),
        ],
        check=True,
    )


def _write_roformer_bundle(
    stems_root: Path,
    stable_id: str,
    *,
    mix_path: Path,
    vocals_path: Path,
    instrumental_path: Path,
) -> None:
    bundle_dir = stems_root / stable_id
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)
    sample_rate, channels, frame_count = _ffprobe_audio(mix_path)
    mix_probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(mix_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    mix_duration_sec = float(mix_probe.stdout.strip())
    vocals_wav = bundle_dir / "vocals.wav"
    instrumental_wav = bundle_dir / "instrumental.wav"
    _transcode_wav_aligned(
        vocals_path, vocals_wav, sample_rate=sample_rate, duration_sec=mix_duration_sec
    )
    _transcode_wav_aligned(
        instrumental_path,
        instrumental_wav,
        sample_rate=sample_rate,
        duration_sec=mix_duration_sec,
    )
    mix_digest = hashlib.sha256(mix_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 3,
        "stable_id": stable_id,
        "layout": "roformer2",
        "model": {"name": "mel-band-roformer", "version": "1"},
        "source": {"path": str(mix_path), "sha256": mix_digest},
        "audio": {
            "sample_rate": sample_rate,
            "frame_count": frame_count,
            "channels": channels,
        },
        "files": {part: f"{part}.wav" for part in ROFORMER_PARTS},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _duration_ms(path: Path) -> int:
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return round(float(probe.stdout.strip()) * 1000)


def _upsert_track(
    writer: StateWriter,
    *,
    stable_id: str,
    title: str,
    audio_path: Path,
) -> None:
    digest = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    writer.upsert_track(
        stable_id=stable_id,
        stable_id_tier="inferred",
        title=title,
        artists=["Fixture"],
        album=None,
        isrc=None,
        duration_ms=_duration_ms(audio_path),
        file_path=str(audio_path),
        content_hash=digest,
        audio_hash=digest,
    )


def build(data_dir: Path) -> SilenceExpectedAudibleFixture:
    refuse_protected_target(data_dir)
    data_dir = data_dir.resolve()
    marker = data_dir / REVISION_MARKER
    state_db_path = data_dir / "state" / "state.db"
    # Always rebuilt: a surviving copy is not release evidence until every copied
    # and transcoded byte is re-proven, and rebuilding is the cheaper proof.
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    audio_dir = data_dir / "audio"
    audio_dir.mkdir()

    sources = {name: _verified_repo_file(rel) for name, rel in SOURCE_PATHS.items()}
    mixture_dst = audio_dir / "mixture.m4a"
    silent_dst = audio_dir / "silent-intro.mp3"
    shutil.copy2(sources["mixture"][0], mixture_dst)
    shutil.copy2(sources["silentIntro"][0], silent_dst)

    _state_cli(data_dir, "init")
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="e2e-silence-expected-audible")
    try:
        _upsert_track(
            writer,
            stable_id=MIXTURE_STABLE_ID,
            title="silence e2e mixture",
            audio_path=mixture_dst,
        )
        _upsert_track(
            writer,
            stable_id=SILENT_INTRO_STABLE_ID,
            title="silence e2e silent intro",
            audio_path=silent_dst,
        )
    finally:
        writer.close()
        conn.close()

    _write_roformer_bundle(
        data_dir / "state" / "stems",
        MIXTURE_STABLE_ID,
        mix_path=mixture_dst,
        vocals_path=sources["vocals"][0],
        instrumental_path=sources["instrumental"][0],
    )
    marker.write_text(f"{FIXTURE_REVISION}\n", encoding="utf-8")
    return SilenceExpectedAudibleFixture(
        mixture_stable_id=MIXTURE_STABLE_ID,
        silent_intro_stable_id=SILENT_INTRO_STABLE_ID,
        revision=FIXTURE_REVISION,
    )


def write_manifest(path: Path, fixture: SilenceExpectedAudibleFixture) -> None:
    payload = {
        "revision": fixture.revision,
        "mixture_stable_id": fixture.mixture_stable_id,
        "silent_intro_stable_id": fixture.silent_intro_stable_id,
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
        f"[silence-expected-audible-fixture] revision={fixture.revision} "
        f"mixture={fixture.mixture_stable_id} silent_intro={fixture.silent_intro_stable_id} "
        f"data_dir={args.data_dir}"
    )
    if args.manifest is not None:
        write_manifest(args.manifest, fixture)
        print(f"[silence-expected-audible-fixture] manifest={args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
