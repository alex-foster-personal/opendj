"""Portable audio_path: --audio-root hydration and payload-sha256 validation (NATIVE-06).

Codex P1 BLOCKING (PR #1536, thread on ``score.py:263``): the manifest's
``audio_path`` was the BUILDER's own absolute Mac path, so a bundle built here
could not score on nucbox or agentbox at all - that exact path exists on no
other host. The fix records a declared ``audio_root`` in the manifest and
stores every track's ``audio_path`` relative to it;
``apps.analysis_waveform.score`` hydrates the pair back into an absolute path
at run time, defaulting ``--audio-root`` to the manifest's own recorded root
so a host with its own copy of the library at a different mount point can
still score without a rebuild.

Codex P2 NON-BLOCKING (PR #1536, thread on ``score.py:265``): the scorer
compared a WHOLE-FILE sha256 against the manifest, so a retag (rekordbox or
Mixed In Key rewriting ID3 tags on analysis) flipped the digest and the track
silently stopped scoring even though nothing a listener hears changed. The fix
compares :func:`apps.shared.hashing.sha256_audio_payload` (tags stripped for
mp3) instead, so a retag keeps scoring and only a real content change under
the same ``stable_id`` is refused.

Real audio throughout, same construction style as
``tests/analysis_waveform/test_scorer_gates.py`` (each fixture file in this
package builds its own clip rather than importing another test module's
helper): a WAV with content in all three bands, encoded to mp3 through ffmpeg
so the retag case has real ID3 tags to strip. The ID3-stripping logic itself
is pinned independently, without needing real audio or ffmpeg, in
``tests/shared/test_hashing.py``; this file only has to prove ``score.py``
actually USES that function to gate scoring.

  - [if] audio_root moves to another host [then] --audio-root scores it there, [else stop].
  - [if] --audio-root is omitted [then] the manifest's own recorded root is used, [else stop].
  - [if] a track's audio is missing under --audio-root [then] the run fails loud, [else stop].
  - [if] --audio-root is not a real directory [then] the run fails loud first, [else stop].
  - [if] an mp3's ID3 tags change but its payload does not [then] it still scores, [else stop].
  - [if] an mp3's audio payload changes [then] the track reports unmeasured, [else stop].
"""

from __future__ import annotations

import json
import math
import re
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from apps.analysis_waveform import decode, score
from apps.analysis_waveform.bands import _downsample_max
from apps.shared.hashing import sha256_audio_payload, sha256_file

pytestmark = [pytest.mark.requirement("NATIVE-06"), pytest.mark.requires_ffmpeg]

SAMPLE_RATE_HZ = 44_100
CLIP_S = 12.0
TRUTH_COLUMNS = 1200
TRUTH_SCALE = 127.0
STABLE_IDS: tuple[str, ...] = ("clip-a", "clip-b")


def _write_three_band_clip(path: Path, *, seed: int) -> None:
    """A real WAV with content in all three bands (see test_scorer_gates.py for
    why: a constant band has an undefined correlation, and a time-symmetric
    envelope would trip the scorer's own negative control on the fixture)."""
    from random import Random

    blocks = Random(seed)
    block_samples = SAMPLE_RATE_HZ // 10
    frames = bytearray()
    envelope = 0.0
    for i in range(int(SAMPLE_RATE_HZ * CLIP_S)):
        if i % block_samples == 0:
            envelope = 0.15 + 0.8 * blocks.random()
        t = i / SAMPLE_RATE_HZ
        value = envelope * (
            0.5 * math.sin(2 * math.pi * 60.0 * t)
            + 0.3 * math.sin(2 * math.pi * 1_000.0 * t)
            + 0.2 * math.sin(2 * math.pi * 12_000.0 * t)
        )
        frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32000))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(bytes(frames))


def _encode_mp3(wav_path: Path, mp3_path: Path) -> None:
    subprocess.run(
        [
            decode._resolve_ffmpeg(),
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(wav_path),
            "-codec:a",
            "libmp3lame",
            "-qscale:a",
            "2",
            str(mp3_path),
        ],
        check=True,
        capture_output=True,
    )


def _prepend_id3v2(path: Path, *, title: bytes) -> None:
    """Rewrite ``path`` with a new leading ID3v2 tag, simulating a retag in
    place without touching a single byte of the audio payload after it."""
    size = len(title)
    synchsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    header = b"ID3" + bytes([4, 0, 0]) + synchsafe
    original = path.read_bytes()
    path.write_bytes(header + title + original)


def _truth_bands(audio: Path) -> dict[str, list[float]]:
    reduced = _downsample_max(decode.decode_peaks(audio), TRUTH_COLUMNS)
    return {
        name: [round(float(v) / 255.0 * TRUTH_SCALE) for v in reduced[:, index]]
        for index, name in enumerate(decode.BAND_NAMES)
    }


@pytest.fixture(scope="module")
def source_mp3s(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Two real, DISTINCT mp3 clips, encoded once and reused read-only.

    Distinct content per stable_id (different seeds) matters for the
    re-encode test below: it swaps ``clip-a``'s bytes for a clip that already
    exists as ``clip-b``'s, so the truth tag and stable_id stay put while the
    audio underneath genuinely differs - the scenario a repair or relink
    produces, not a file that merely does not exist.
    """
    src = tmp_path_factory.mktemp("audio-root-source")
    paths: dict[str, Path] = {}
    for index, stable_id in enumerate(STABLE_IDS):
        wav = src / f"{stable_id}.wav"
        mp3 = src / f"{stable_id}.mp3"
        _write_three_band_clip(wav, seed=20260908 + index)
        _encode_mp3(wav, mp3)
        paths[stable_id] = mp3
    return paths


def _materialize_bundle(root: Path, audio_dir: Path, source_mp3s: dict[str, Path]) -> Path:
    """A fresh two-track bundle: ``audio_dir`` holds a COPY of the source mp3s,
    ``root`` holds the (audio-free) bundle directory itself, manifest paths are
    relative to ``audio_dir``, recorded as the manifest's own ``audio_root``."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    bundle = root / "v1"
    (bundle / "truth").mkdir(parents=True, exist_ok=True)
    tracks = []
    for stable_id, source_mp3 in source_mp3s.items():
        mp3 = audio_dir / f"{stable_id}.mp3"
        shutil.copy2(source_mp3, mp3)
        truth_bytes = json.dumps(
            {
                "stable_id": stable_id,
                "tag": "PWV6",
                "scale": TRUTH_SCALE,
                "columns": TRUTH_COLUMNS,
                "bands": _truth_bands(mp3),
            }
        ).encode("utf-8")
        (bundle / "truth" / f"{stable_id}.json").write_bytes(truth_bytes)
        tracks.append(
            {
                "stable_id": stable_id,
                "truth_file": f"truth/{stable_id}.json",
                "audio_path": f"{stable_id}.mp3",
                "audio_sha256": sha256_file(mp3),
                "payload_sha256": sha256_audio_payload(mp3),
            }
        )
    manifest = {
        "bundle": score.EXPECTED_BUNDLE,
        "truth_tag": "PWV6",
        "audio_root": str(audio_dir),
        "tracks": tracks,
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    lines = [
        f"{sha256_file(p).removeprefix('sha256:')}  {p.relative_to(bundle).as_posix()}"
        for p in sorted(bundle.rglob("*"))
        if p.is_file() and p.name != score.CHECKSUM_FILE
    ]
    (bundle / score.CHECKSUM_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return bundle


# ----- --audio-root hydration ----------------------------------------------


def test_scoring_works_after_the_audio_root_moves(
    tmp_path: Path, source_mp3s: dict[str, Path]
) -> None:
    bundle = _materialize_bundle(tmp_path / "bundle", tmp_path / "build-root", source_mp3s)

    moved_root = tmp_path / "moved-root"
    moved_root.mkdir()
    for mp3 in (tmp_path / "build-root").glob("*.mp3"):
        shutil.copy2(mp3, moved_root / mp3.name)

    report = score.run(bundle, audio_root=moved_root)
    assert report["denominators"]["scored"] == 2, report["unmeasured_reasons"]
    assert score.control_failures(report) == []


def test_default_audio_root_comes_from_the_manifest_when_the_flag_is_omitted(
    tmp_path: Path, source_mp3s: dict[str, Path]
) -> None:
    bundle = _materialize_bundle(tmp_path / "bundle", tmp_path / "build-root", source_mp3s)

    report = score.run(bundle)  # no --audio-root: falls back to the manifest's own
    assert report["denominators"]["scored"] == 2, report["unmeasured_reasons"]


def test_the_cli_scores_after_the_audio_root_moves(
    tmp_path: Path, source_mp3s: dict[str, Path]
) -> None:
    bundle = _materialize_bundle(tmp_path / "bundle", tmp_path / "build-root", source_mp3s)
    moved_root = tmp_path / "moved-root"
    moved_root.mkdir()
    for mp3 in (tmp_path / "build-root").glob("*.mp3"):
        shutil.copy2(mp3, moved_root / mp3.name)

    assert score.main(["--bundle", str(bundle), "--audio-root", str(moved_root)]) == 0


def test_a_missing_file_under_an_explicit_audio_root_fails_loud(
    tmp_path: Path, source_mp3s: dict[str, Path]
) -> None:
    bundle = _materialize_bundle(tmp_path / "bundle", tmp_path / "build-root", source_mp3s)

    incomplete_root = tmp_path / "incomplete-root"
    incomplete_root.mkdir()
    shutil.copy2(tmp_path / "build-root" / "clip-a.mp3", incomplete_root / "clip-a.mp3")
    # clip-b is deliberately left out: this is the FIRST missing file in
    # manifest order (tracks are written stable_id-sorted at build time).

    resolved = incomplete_root / "clip-b.mp3"
    with pytest.raises(SystemExit, match=re.escape(str(resolved))):
        score.run(bundle, audio_root=incomplete_root)


def test_a_nonexistent_audio_root_fails_loud_before_any_track_is_touched(
    tmp_path: Path, source_mp3s: dict[str, Path]
) -> None:
    bundle = _materialize_bundle(tmp_path / "bundle", tmp_path / "build-root", source_mp3s)

    ghost_root = tmp_path / "does-not-exist"
    with pytest.raises(SystemExit, match="is not a directory"):
        score.run(bundle, audio_root=ghost_root)


# ----- payload sha256 (retag vs re-encode) ----------------------------------


def test_a_retagged_mp3_still_scores(tmp_path: Path, source_mp3s: dict[str, Path]) -> None:
    build_root = tmp_path / "build-root"
    bundle = _materialize_bundle(tmp_path / "bundle", build_root, source_mp3s)
    _prepend_id3v2(build_root / "clip-a.mp3", title=b"a brand new title, much longer than before")

    report = score.run(bundle)
    row = next(r for r in report["tracks"] if r["stable_id"] == "clip-a")
    assert "unmeasured" not in row, row
    assert report["denominators"]["scored"] == 2, report["unmeasured_reasons"]


def test_a_reencoded_mp3_is_reported_unmeasured_not_silently_scored(
    tmp_path: Path, source_mp3s: dict[str, Path]
) -> None:
    build_root = tmp_path / "build-root"
    bundle = _materialize_bundle(tmp_path / "bundle", build_root, source_mp3s)
    # Same stable_id, same truth tag, DIFFERENT bytes underneath - swap in the
    # other clip's already-encoded audio, exactly what a repair or relink
    # leaves behind (Codex P1 on build_waveform_bundle.py:105, restated for
    # the scorer's own read side).
    shutil.copy2(source_mp3s["clip-b"], build_root / "clip-a.mp3")

    report = score.run(bundle)
    row = next(r for r in report["tracks"] if r["stable_id"] == "clip-a")
    assert row.get("unmeasured", "").startswith("audio payload sha256 differs"), row
    assert report["denominators"]["scored"] == 1, report["unmeasured_reasons"]
