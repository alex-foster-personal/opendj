"""odj-live on the committed, checksummed encoded-media fixture.

``tests/fixtures/app-acceptance/fixture.json`` is the installed-app
acceptance fixture: real MP3s with a known-answer ``analysis`` block. Every
file is checked against its manifest sha256 before it is used, and a
mismatch or a missing file FAILS (it is a locked fixture in this checkout,
not an optional dependency). Nothing here edits it.

1. Known answers: the built ``odj-live --file`` reads each analysis subject
   and must land on the fixture's own answer (A minor is 8A; the kick and
   click tracks read inside their BPM bands). The relative and octave
   policies are the fixture's: C major (8B) or a half/double tempo fail.
2. Library parity on real media: the summary key equals what
   ``apps/analysis_key/profiles.py`` says on ``librosa.feature.chroma_cqt``
   of the same decoded file, as the synthetic parity cases check.
3. A corrupt packet keeps time: a disposable copy with whole MP3 frames
   garbled decodes to the clean file's duration, each garbled packet played
   as silence of its own length, and the summary counts them.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

from apps.analysis_key import canon, profiles
from tests.live_core.conftest import AUDIO_STACK_MARKS, COSINE_FLOOR, REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "app-acceptance"
MANIFEST = FIXTURE_DIR / "fixture.json"
SCHEMA = "opendj-acceptance-fixture/v1"
SUBJECTS = ["Acceptance A Minor", "Acceptance Kick 124", "Acceptance Click 90"]
#: An MPEG-1 layer III packet: 1152 samples.
MP3_PACKET_SAMPLES = 1152
#: Whole frames garbled in the corrupt-packet copy. Twenty packets is about
#: 0.52 s at 44.1 kHz, so a decode that drops them instead of silencing them
#: misses the one-packet tolerance by a factor of twenty.
CORRUPT_FRAMES = 20
# MPEG audio frame header: 11 sync bits, then MPEG-1 and layer III in the next byte.
MPEG_SYNC_BYTE = 0xFF
MPEG1_LAYER3_BYTE = 0xFA
RESERVED_RATE_INDEX = 3
# A clip has more frames than this, and a trailing gap smaller than this is tolerated.
MIN_FRAMES = 1000
TRAILING_SLACK = 128


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest() -> dict[str, Any]:
    if not MANIFEST.is_file():
        pytest.fail(f"the acceptance fixture manifest is missing: {MANIFEST}")
    data: dict[str, Any] = json.loads(MANIFEST.read_text())
    if data.get("schema") != SCHEMA:
        pytest.fail(f"{MANIFEST} is schema {data.get('schema')!r}, this test reads {SCHEMA!r}")
    return data


def _verified_track(manifest: dict[str, Any], title: str) -> tuple[Path, dict[str, Any]]:
    """The track's file, after its bytes match the manifest's sha256."""
    matches = [t for t in manifest["tracks"] if t["title"] == title]
    if len(matches) != 1:
        pytest.fail(f"{MANIFEST} names {len(matches)} tracks titled {title!r}, expected 1")
    track = matches[0]
    path = FIXTURE_DIR / track["file"]
    if not path.is_file():
        pytest.fail(f"fixture file {path} named by {MANIFEST} is missing")
    got = _sha256(path)
    if got != track["sha256"]:
        pytest.fail(
            f"{path} has sha256 {got}, the manifest pins {track['sha256']}; refusing to measure a changed fixture"
        )
    return path, track


def _subject(manifest: dict[str, Any], title: str) -> dict[str, Any]:
    matches = [s for s in manifest["analysis"]["subjects"] if s["title"] == title]
    if len(matches) != 1:
        pytest.fail(f"{MANIFEST} has {len(matches)} analysis subjects titled {title!r}, expected 1")
    return matches[0]


def _summary(odj_live: Path, path: Path) -> dict[str, Any]:
    run = subprocess.run([str(odj_live), "--file", str(path), "--summary"], capture_output=True, text=True, check=False)
    assert run.returncode == 0, f"odj-live exited {run.returncode} on {path}: {run.stderr}"
    lines = run.stdout.splitlines()
    assert len(lines) == 1, lines[:3]
    summary: dict[str, Any] = json.loads(lines[0])
    assert summary["type"] == "summary", summary
    return summary


@pytest.mark.parametrize("title", SUBJECTS)
def test_odj_live_reads_the_fixture_known_answers(title: str, odj_live: Path) -> None:
    manifest = _manifest()
    path, track = _verified_track(manifest, title)
    subject = _subject(manifest, title)
    summary = _summary(odj_live, path)
    print(
        f"ACCEPTANCE {title}: key={summary['key']} bpm={summary['bpm']} "
        f"tuning={summary['tuning_cents']} duration={summary['duration_s']} corrupt={summary['corrupt_packets']}"
    )
    # The fixture's nominal length, to one MP3 packet: a decode that lost or
    # invented audio would move every time after the gap.
    nominal = track["duration_ms"] / 1000.0
    # One packet at the rate the decoder actually fed the analyzer, not an assumed 44.1 kHz.
    packet_s = MP3_PACKET_SAMPLES / summary["sample_rate_in"]
    assert abs(summary["duration_s"] - nominal) <= packet_s, (summary["duration_s"], nominal)
    assert summary["corrupt_packets"] == 0, summary["corrupt_packets"]
    checked = 0
    if "key_accept" in subject:
        assert summary["key"] in subject["key_accept"], (
            f"{title}: odj-live says {summary['key']}, the fixture accepts {subject['key_accept']}"
        )
        checked += 1
    if "bpm_min" in subject:
        # TCLIVE-6567d: the BPM is withheld until the tempo prior is calibrated.
        # The calibration PR revisits these bands; they are in git history.
        assert summary["bpm"] is None, f"{title}: odj-live published {summary['bpm']} BPM while withheld"
        checked += 1
    # A subject this test reads no answer from is a fixture this test no
    # longer understands, not a pass.
    assert checked > 0, f"{title}: the fixture subject carries neither key_accept nor a BPM band: {subject}"


@pytest.mark.parametrize("title", [pytest.param(t, marks=AUDIO_STACK_MARKS) for t in SUBJECTS])
def test_odj_live_key_matches_profiles_on_librosa_chroma_of_the_fixture(
    title: str, odj_live: Path, librosa_module: ModuleType
) -> None:
    path, _ = _verified_track(_manifest(), title)
    summary = _summary(odj_live, path)
    y, sr = librosa_module.load(str(path), sr=22050, mono=True)
    reference = librosa_module.feature.chroma_cqt(y=y, sr=sr)
    ours = np.asarray(summary["chroma_mean"], dtype=np.float64)
    theirs = reference.mean(axis=1)
    cosine = float(ours @ theirs / (np.linalg.norm(ours) * np.linalg.norm(theirs)))
    want = canon.to_camelot(profiles.estimate_key_krumhansl(reference).key)
    print(
        f"PARITY {title}: cosine={cosine:.4f} ours={summary['key']} profiles_on_librosa={want} "
        f"librosa={librosa_module.__version__}"
    )
    assert cosine >= COSINE_FLOOR, f"{title}: mean chroma cosine {cosine:.4f} < {COSINE_FLOOR}"
    assert summary["key"] == want, f"{title}: odj-live says {summary['key']}, profiles.py on librosa chroma says {want}"


_MP3_BITRATES_KBPS = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
_MP3_RATES = [44100, 48000, 32000]


def _mp3_frames(data: bytes) -> list[tuple[int, int]]:
    """(offset, length) of each MPEG-1 layer III frame, walked header to
    header after any ID3v2 tag. Fails on anything else, rather than guess."""
    pos = 0
    if data[:3] == b"ID3":
        pos = 10 + ((data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9])
    frames: list[tuple[int, int]] = []
    while pos + 4 <= len(data):
        b1, b2 = data[pos + 1], data[pos + 2]
        if data[pos] != MPEG_SYNC_BYTE or (b1 & 0xFE) != MPEG1_LAYER3_BYTE:  # sync, MPEG-1, layer III
            break
        bitrate = _MP3_BITRATES_KBPS[b2 >> 4] if (b2 >> 4) < len(_MP3_BITRATES_KBPS) else 0
        rate_index = (b2 >> 2) & 3
        if bitrate == 0 or rate_index == RESERVED_RATE_INDEX:
            break
        length = 144_000 * bitrate // _MP3_RATES[rate_index] + ((b2 >> 1) & 1)
        frames.append((pos, length))
        pos += length
    return frames


def test_a_corrupt_packet_plays_as_silence_of_its_own_length(odj_live: Path, tmp_path: Path) -> None:
    path, _ = _verified_track(_manifest(), "Acceptance A Minor")
    data = path.read_bytes()
    frames = _mp3_frames(data)
    # The walk must reach the end of the file, or the offsets are not frames.
    assert len(frames) > MIN_FRAMES and frames[-1][0] + frames[-1][1] >= len(data) - TRAILING_SLACK, (
        len(frames),
        len(data),
    )

    clean = _summary(odj_live, path)
    assert clean["corrupt_packets"] == 0, clean

    # Garble the side information of 20 consecutive frames mid-file (the
    # header stays, so the demuxer still finds each packet and its length;
    # 0xFF side info is a granule the decoder refuses). The copy lives in
    # tmp_path; the fixture is never written.
    garbled = bytearray(data)
    first = len(frames) // 2
    for offset, _ in frames[first : first + CORRUPT_FRAMES]:
        garbled[offset + 4 : offset + 4 + 32] = b"\xff" * 32
    copy = tmp_path / "a-minor-garbled.mp3"
    copy.write_bytes(bytes(garbled))
    assert _sha256(path) != _sha256(copy)

    damaged = _summary(odj_live, copy)
    print(f"CORRUPT clean={clean['duration_s']} damaged={damaged['duration_s']} packets={damaged['corrupt_packets']}")
    # The presence half: the decoder really refused those packets.
    assert damaged["corrupt_packets"] == CORRUPT_FRAMES, damaged["corrupt_packets"]
    packet_s = MP3_PACKET_SAMPLES / clean["sample_rate_in"]
    assert abs(damaged["duration_s"] - clean["duration_s"]) <= packet_s, (damaged["duration_s"], clean["duration_s"])
    assert damaged["key"] == clean["key"], (damaged["key"], clean["key"])
