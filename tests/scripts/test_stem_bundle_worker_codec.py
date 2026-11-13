"""Guard: scripts/stem_bundle_worker.py's stem output codec follows the
source-extension policy (apps/stems/stem_size_policy.py) instead of the old
hardcoded WAV output (issue #1497).

_stem_file_suffix and _manifest_dict are pure (no filesystem access, no
torch/demucs) so this guard runs on any box, with no GPU and no real audio
fixture required -- the full separation path (write_bundle) needs a
GPU/torch/demucs stack this suite does not have. The manifest round-trip
tests below DO touch the filesystem (real media + manifest.json under
tmp_path) to exercise the real production reader
(apps.stems.artifacts.load_stem_bundle) against the schema v3
manifest shape this worker writes, since that reader is the actual contract
this fix must satisfy and _load_v3_bundle never reads file headers (audio
alignment comes from the manifest's own audio block, not the files).

Codex review (PR #1663) flagged an earlier draft that wrote fabricated
``b"not-real-audio"`` bytes for the stem parts: a reader that only trusts the
filename suffix and manifest metadata stays green even over corrupt or
undecodable media, so it gave no acceptance evidence for the codec path
itself. A second draft built real media by piping a stdlib ``wave`` PCM16 WAV
through the ``ffmpeg`` binary directly for FLAC, bypassing the production
``worker._encode_flac_bytes`` (tensor -> FLAC) function entirely -- so the
test stayed green even if that function itself emitted corrupt media, and its
stub manifest's ``frame_count`` (1000) did not match the audio it declared
that count for (the WAV helper's own default was 4410 frames), so no test
here was actually checking alignment between what the manifest claims and
what the encoder produced.

This version calls the REAL production ``worker._encode_stem_part`` for both
codec round trips -- the actual chaining function ``write_bundle`` itself
calls, not a hand-reimplemented ``_stem_array`` -> ``_encode_mp3``
substitute, so the mp3 rung-1/rung-2 bitrate-ladder retry
(apps/stems/stem_size_policy.py's ``lossy_stem_exceeds_source`` /
``mp3_rung2_cbr_kbps``) is the real code path, not skipped. That function
takes the numpy [channels, frames] array ``worker._tensor_to_numpy``
produces from a Demucs tensor, so it runs here on a real numpy array with no
stand-in for a tensor (Codex P1 on #6820); torch stays out of this repo's
venv per (private path).
A real Demucs tensor from ``write_bundle``'s own GPU/torch inference path is
UNAVAILABLE at this test layer by the same rule (no torch/demucs in the repo
venv, ever) -- this suite has never claimed to exercise that half, and
``write_bundle`` itself (the full GPU/demucs path) is separately noted in
this script's own MINI-PRD as not run end-to-end on this box. soundfile is a
real dependency this needs (the ``analysis`` extra, ``pyproject.toml`` line
99), so these tests are ``requires_audio_stack``-gated: UNAVAILABLE with a
stated reason on a bare ``uv sync`` dev venv, and real, measured evidence in
CI, which installs ``requirements.txt`` (soundfile is pinned there, line
159) -- the same honest-skip convention ``tests/conftest.py`` already uses
for the analysis backend's own end-to-end test. ``FRAMES``/``SAMPLE_RATE``/
``CHANNELS`` below are the single source of truth for both the tensor fed to
the real encoder and the manifest's declared ``audio`` block, so the two
cannot drift the way the 1000-vs-4410 mismatch did. ``ffprobe``'s own
decoded duration for each written part, checked against
``FRAMES / SAMPLE_RATE``, is the alignment acceptance evidence -- via
ffmpeg/ffprobe's decoder, not the browser's WebAudio ``decodeAudioData``
that ``apps/webui/frontend/src/lib/rb/stem-graph.ts``'s
``validateStemBufferAlignment`` and ``audio-engine.svelte.ts`` actually gate
playback on. That frame-exact, browser-decoded check is UNAVAILABLE from
this pytest layer (a different decoder, in a different runtime, reachable
only from a browser/Playwright context) and is NOT asserted here; see this
module's PR review thread for the open question of whether a lossy mp3
stem is guaranteed to browser-decode to the source's exact frame count.

FLAC is lossless (soundfile writes exact PCM): measured exact (0.0000s
delta) both locally and in CI, so ``FLAC_DURATION_TOLERANCE_S`` stays tight
(10ms, symmetric). mp3 is lossy -- LAME pads its output to whole
1152-sample MPEG-1 Layer III frames and adds an encoder start delay, so a
naive duration reader (ffprobe's ``format=duration``, no Xing/LAME header
skip) reads mp3 output as never SHORTER than the source and up to about two
frames LONGER; an earlier version of this fixture measured that directly --
exact locally (ffmpeg 8.0.1) but a real 30.6ms delta (~1350 samples, just
over one 1152-sample frame) against CI's apt-get-installed ffmpeg, still
genuinely decodable audio, not corruption. ``MP3_MAX_PADDING_S`` (two frames,
~52ms) is derived from that frame size, not fitted to the one measurement,
so the bound is asymmetric: ``[expected, expected + MP3_MAX_PADDING_S]``,
never allowing a SHORTER decode (lost audio would still fail hard).

  - [if] the source extension is .mp3 [then] the stem suffix is .mp3, [else stop]
  - [if] a v3 bundle's mp3 parts are encoded by the real production
    ``_encode_stem_part`` chain (``_stem_array`` -> ``_encode_mp3``,
    including the rung-1/rung-2 bitrate-ladder retry) [then] each part's
    ffprobe duration falls in ``[FRAMES / SAMPLE_RATE, FRAMES / SAMPLE_RATE +
    MP3_MAX_PADDING_S]``, [else stop]
  - [if] a v3 bundle's flac parts are encoded by the real production
    ``_encode_stem_part`` chain [then] each part's ffprobe duration matches
    ``FRAMES / SAMPLE_RATE`` within ``FLAC_DURATION_TOLERANCE_S``, [else stop]
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import scripts.stem_bundle_worker as worker
from apps.stems.artifacts import load_stem_bundle
from apps.stems.stem_size_policy import UnknownSourceFormatError

pytestmark = pytest.mark.requirement("STEM-02")

FFMPEG = os.environ.get("MDT_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("MDT_FFPROBE", "ffprobe")

# Single source of truth for the codec round-trip tests: fed into the real
# encoder AND the manifest's declared audio block, so they cannot disagree.
# 1 second, not the original 4410-frame (0.1s) draft: at 0.1s, LAME frame
# padding/encoder delay (see _encode_mp3's own docstring) is a large
# fraction of the clip, so a genuine encoder/decoder-delay difference across
# ffmpeg builds and a real corruption become hard to tell apart by duration
# alone. At 1s both are unambiguous.
SAMPLE_RATE = 44100
CHANNELS = 2
FRAMES = SAMPLE_RATE
# FLAC is lossless (soundfile writes exact PCM): measured exact (0.0000s
# delta) both locally (ffmpeg 8.0.1) and in CI, so a tight tolerance is real
# evidence, not acceptance theater.
FLAC_DURATION_TOLERANCE_S = 0.01
# mp3 is lossy: LAME encodes fixed 1152-sample MPEG-1 Layer III frames, so a
# naive duration reader (no Xing/LAME header skip) reads real encoder start
# delay plus padding to the next frame boundary as extra audio -- never
# fewer samples than the source, up to about one frame's worth per delay
# component. Two frames is a derived physical bound, not a fitted fudge
# factor: CI's apt-get-installed ffmpeg measured a real 30.6ms delta
# (~1350 samples, just over one 1152-sample frame) against a source-exact
# local measurement (ffmpeg 8.0.1), comfortably inside this bound.
MP3_FRAME_SAMPLES = 1152
MP3_MAX_PADDING_S = 2 * MP3_FRAME_SAMPLES / SAMPLE_RATE


def test_mp3_source_gets_mp3_stem_suffix() -> None:
    assert worker._stem_file_suffix(Path("track.mp3")) == ".mp3"


def test_m4a_source_gets_mp3_stem_suffix() -> None:
    assert worker._stem_file_suffix(Path("track.m4a")) == ".mp3"


def test_flac_source_gets_mp3_stem_suffix() -> None:
    """STEM-8231: four FLAC parts cannot fit inside one source, so MP3."""
    assert worker._stem_file_suffix(Path("track.flac")) == ".mp3"


def test_wav_source_gets_mp3_stem_suffix() -> None:
    assert worker._stem_file_suffix(Path("track.wav")) == ".mp3"


def test_unrecognised_source_extension_raises() -> None:
    with pytest.raises(UnknownSourceFormatError):
        worker._stem_file_suffix(Path("track.ogg"))


def _silence_tensor(*, channels: int = CHANNELS, frames: int = FRAMES) -> Any:
    """A genuinely all-zero (silent, decodable) (channels, frames) float32
    array: the layout ``worker._tensor_to_numpy`` hands the encoder."""
    return np.zeros((channels, frames), dtype=np.float32)


def _ffprobe_duration_s(path: Path) -> float:
    """ffprobe's own reported duration: acceptance evidence that ``path`` is
    genuinely decodable media, not just a suffix a reader trusted blindly."""
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return float(proc.stdout.strip())


def _write_stub_bundle(
    tmp_path: Path, stable_id: str, *, ext: str, codec: str, part_bytes: bytes
) -> Path:
    """Write a schema v3 bundle (real, decodable part bytes shared across all
    4 parts, real manifest.json) so the production reader AND ffprobe can be
    exercised without a GPU or a real separated track.

    ``audio`` is built from the SAME ``SAMPLE_RATE``/``FRAMES``/``CHANNELS``
    constants the caller fed to the real encoder, not independent literals --
    the earlier draft's declared ``frame_count`` (1000) never matched the
    4410 frames it actually encoded, so this manifest could not have caught a
    real misalignment."""
    bundle_dir = tmp_path / stable_id
    bundle_dir.mkdir()
    for name in worker.STEM_PARTS:
        (bundle_dir / f"{name}{ext}").write_bytes(part_bytes)
    manifest = worker._manifest_dict(
        stable_id=stable_id,
        audio_path=Path(f"/tmp/source{'.mp3' if codec == 'mp3' else '.flac'}"),
        source_sha256="0" * 64,
        ext=ext,
        output_codec=codec,
        audio={"sample_rate": SAMPLE_RATE, "frame_count": FRAMES, "channels": CHANNELS},
        size_policy={
            "source_bytes": 12345,
            "source_kbps": 256.0,
            "mp3_settings": None,
            "part_bytes": {name: len(part_bytes) for name in worker.STEM_PARTS},
            "violations": [],
        },
    )
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def _assert_parts_decode_to_declared_duration(
    root: Path, stable_id: str, ext: str, *, min_duration_s: float, max_duration_s: float
) -> None:
    """Alignment evidence: what each part file ACTUALLY decodes to (ffprobe,
    reading the real media header/frames) must fall within
    [min_duration_s, max_duration_s] of what the manifest DECLARES
    (FRAMES / SAMPLE_RATE) -- the cross-check the reader itself intentionally
    skips (it trusts the manifest's own audio block, see module docstring),
    so a fixture that does not enforce it here proves nothing about the real
    encoder's alignment."""
    for name in worker.STEM_PARTS:
        actual_duration_s = _ffprobe_duration_s(root / stable_id / f"{name}{ext}")
        assert min_duration_s <= actual_duration_s <= max_duration_s, (
            f"{name}{ext}: expected [{min_duration_s:.4f}s, {max_duration_s:.4f}s], "
            f"ffprobe decoded {actual_duration_s:.4f}s"
        )


@pytest.mark.requires_audio_stack
@pytest.mark.requires_ffmpeg
def test_mp3_schema_v3_manifest_round_trips_through_real_reader(tmp_path: Path) -> None:
    # The real production chaining function write_bundle itself calls, not a
    # hand-reimplemented _stem_array -> _encode_mp3 substitute:
    # this exercises the rung-1/rung-2 bitrate-ladder retry too.
    # source_bytes is set generously above what a silent 1s mp3 can encode
    # to, so rung 1 holds and the assertion below confirms that.
    mp3_bytes, rung2_settings = worker._encode_stem_part(
        _silence_tensor(),
        SAMPLE_RATE,
        codec="mp3",
        part="vocals",
        mp3_settings=None,
        source_bytes=1_000_000,
        source_kbps=256.0,
        out_path=tmp_path / "probe.mp3",
    )
    assert rung2_settings is None, "fixture unexpectedly triggered the mp3 rung-2 ladder"

    root = _write_stub_bundle(
        tmp_path, "teststem-mp3", ext=".mp3", codec="mp3", part_bytes=mp3_bytes
    )
    bundle = load_stem_bundle("teststem-mp3", roots=[root])
    assert bundle.media_type == "audio/mpeg"
    assert bundle.layout == "demucs4"
    expected_duration_s = FRAMES / SAMPLE_RATE
    _assert_parts_decode_to_declared_duration(
        root,
        "teststem-mp3",
        ".mp3",
        min_duration_s=expected_duration_s,
        max_duration_s=expected_duration_s + MP3_MAX_PADDING_S,
    )


@pytest.mark.requires_audio_stack
@pytest.mark.requires_ffmpeg
def test_flac_schema_v3_manifest_round_trips_through_real_reader(tmp_path: Path) -> None:
    # Same real production chaining function as the mp3 test above; the
    # codec="flac" branch is the tensor -> FLAC path (soundfile) PR #1663's
    # review flagged as unexercised, not a same-binary ffmpeg substitute.
    flac_bytes, rung2_settings = worker._encode_stem_part(
        _silence_tensor(),
        SAMPLE_RATE,
        codec="flac",
        part="vocals",
        mp3_settings=None,
        source_bytes=1_000_000,
        source_kbps=256.0,
        out_path=tmp_path / "probe.flac",
    )
    assert rung2_settings is None, "flac never re-encodes; rung2 must stay unset"

    root = _write_stub_bundle(
        tmp_path, "teststem-flac", ext=".flac", codec="flac", part_bytes=flac_bytes
    )
    bundle = load_stem_bundle("teststem-flac", roots=[root])
    assert bundle.media_type == "audio/flac"
    assert bundle.layout == "demucs4"
    expected_duration_s = FRAMES / SAMPLE_RATE
    _assert_parts_decode_to_declared_duration(
        root,
        "teststem-flac",
        ".flac",
        min_duration_s=expected_duration_s - FLAC_DURATION_TOLERANCE_S,
        max_duration_s=expected_duration_s + FLAC_DURATION_TOLERANCE_S,
    )


_STEM_INDEX_STABLE_ID = "abcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcd"


def _write_v3_bundle_parts(
    root: Path,
    stable_id: str,
    *,
    ext: str,
    codec: str,
    part_marker: bytes,
) -> None:
    """Synthetic v3 bundle: stub part bytes and a matching manifest."""
    import json

    root.mkdir(parents=True, exist_ok=True)
    files = {name: f"{name}{ext}" for name in worker.STEM_PARTS}
    for name in worker.STEM_PARTS:
        (root / files[name]).write_bytes(part_marker + name.encode("ascii"))
    manifest = {
        "schema_version": 3,
        "stable_id": stable_id,
        "layout": "demucs4",
        "model": {"name": worker.MODEL_NAME, "version": worker.DEMUCS_VERSION},
        "source": {"path": "/music/source.mp3", "sha256": "d" * 64},
        "files": files,
        "audio": {"sample_rate": SAMPLE_RATE, "frame_count": FRAMES, "channels": CHANNELS},
        "codec": codec,
        "size_policy": {"part_bytes": {name: 32 for name in worker.STEM_PARTS}},
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


@pytest.mark.requirement("STEM-12745")
def test_publish_into_place_alone_leaves_the_download_index_stale(tmp_path: Path) -> None:
    """[if] publish_into_place lands without index record [then] cached index stays stale, [else stop]."""
    from apps.cloud.stem_index import MANIFEST_FILENAME, load_cached_index, save_cached_index

    data = tmp_path / "data"
    bundle = data / "state" / "stems" / "indexonly"
    _write_v3_bundle_parts(bundle, "indexonly", ext=".flac", codec="flac", part_marker=b"stale-")
    stale_manifest_sha = "c" * 64
    save_cached_index(
        data,
        {
            "indexonly": {
                **{f"{part}.flac": "d" * 64 for part in worker.STEM_PARTS},
                MANIFEST_FILENAME: stale_manifest_sha,
            }
        },
    )
    staging = bundle.parent / ".stem-indexonly-new"
    _write_v3_bundle_parts(staging, "indexonly", ext=".mp3", codec="mp3", part_marker=b"fresh-")
    worker.publish_into_place(staging, bundle)
    cached = load_cached_index(data)["indexonly"]
    assert cached[MANIFEST_FILENAME] == stale_manifest_sha
    assert "vocals.flac" in cached


@pytest.mark.requirement("STEM-12745")
def test_worker_publish_into_data_dir_refreshes_stale_flac_index_entries(tmp_path: Path) -> None:
    """[if] publish lands in shared stem root [then] index names new parts and manifest sha, [else stop]."""
    import hashlib

    from apps.cloud.stem_index import MANIFEST_FILENAME, load_cached_index, save_cached_index
    from apps.cloud.stem_index_sync import record_published_bundle_if_shared

    data = tmp_path / "data"
    bundle = data / "state" / "stems" / _STEM_INDEX_STABLE_ID
    _write_v3_bundle_parts(
        bundle, _STEM_INDEX_STABLE_ID, ext=".flac", codec="flac", part_marker=b"old-"
    )
    stale = {f"{part}.flac": "f" * 64 for part in worker.STEM_PARTS}
    stale[MANIFEST_FILENAME] = "e" * 64
    save_cached_index(data, {_STEM_INDEX_STABLE_ID: stale})

    staging = bundle.parent / f".stem-{_STEM_INDEX_STABLE_ID}-new"
    _write_v3_bundle_parts(
        staging, _STEM_INDEX_STABLE_ID, ext=".mp3", codec="mp3", part_marker=b"new-"
    )

    worker.publish_into_place(staging, bundle)
    record_published_bundle_if_shared(bundle)

    index = load_cached_index(data)[_STEM_INDEX_STABLE_ID]
    assert set(index) == {f"{part}.mp3" for part in worker.STEM_PARTS} | {MANIFEST_FILENAME}
    assert index[MANIFEST_FILENAME] == hashlib.sha256(
        (bundle / MANIFEST_FILENAME).read_bytes()
    ).hexdigest()
    assert index[MANIFEST_FILENAME] != stale[MANIFEST_FILENAME]


@pytest.mark.requirement("STEM-12745")
def test_worker_publish_outside_data_dir_writes_no_download_index(tmp_path: Path) -> None:
    """[if] --out-dir is outside a data dir [then] no stem-bundle-index.json write, [else stop]."""
    from apps.cloud.stem_index_sync import record_published_bundle_if_shared

    out = tmp_path / "scratch" / "bundle"
    out.parent.mkdir(parents=True)
    staging = tmp_path / "staging"
    _write_v3_bundle_parts(
        staging, "outside-id", ext=".mp3", codec="mp3", part_marker=b"x-"
    )
    worker.publish_into_place(staging, out)
    assert record_published_bundle_if_shared(out) is None
    assert list(tmp_path.rglob("stem-bundle-index.json")) == []


@pytest.mark.requirement("STEM-12745")
@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a directory the user cannot empty")
def test_failed_publish_into_place_leaves_download_index_unchanged(tmp_path: Path) -> None:
    """[if] publish_into_place fails [then] the download index is not upserted, [else stop]."""
    import shutil

    from apps.cloud.stem_index import load_cached_index, save_cached_index
    from apps.cloud.stem_index_sync import record_published_bundle_if_shared
    from apps.stems.bundle_lock import SwapBackupStuckError, swap_backup_path

    data = tmp_path / "data"
    bundle = data / "state" / "stems" / _STEM_INDEX_STABLE_ID
    _write_v3_bundle_parts(
        bundle, _STEM_INDEX_STABLE_ID, ext=".flac", codec="flac", part_marker=b"old-"
    )
    before_index = {"stale": {"vocals.flac": "a" * 64}}
    save_cached_index(data, before_index)

    new_dir = tmp_path / "new"
    shutil.copytree(bundle, new_dir)
    backup = swap_backup_path(bundle)
    backup.parent.mkdir(parents=True)
    shutil.copytree(bundle, backup)
    backup.chmod(0o500)
    backup.parent.chmod(0o500)
    try:
        with pytest.raises(SwapBackupStuckError):
            worker.publish_into_place(new_dir, bundle)
    finally:
        backup.parent.chmod(0o700)
        backup.chmod(0o700)

    assert load_cached_index(data) == before_index


@pytest.mark.requirement("STEM-12745")
def test_write_bundle_records_the_download_index_after_publish() -> None:
    """[if] write_bundle publishes [then] record_published_bundle_if_shared runs after publish_into_place, [else stop]."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(worker.write_bundle)))
    named = sorted(
        (n.func for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)),
        key=lambda f: (f.lineno, f.col_offset),
    )
    calls = [func.id for func in named if isinstance(func, ast.Name)]
    publish_at = calls.index("publish_into_place")
    record_at = calls.index("record_published_bundle_if_shared")
    assert publish_at < record_at


@pytest.mark.requirement("STEM-12753")
def test_the_worker_declares_pydantic_for_the_index_import() -> None:
    """[if] the PEP 723 worker env lacks pydantic [then] the post-publish index import fails, [else stop]."""
    source = Path(worker.__file__).read_text(encoding="utf-8")
    block = source.split("# /// script", 1)[1].split("# ///", 1)[0]
    assert '"pydantic' in block, block
