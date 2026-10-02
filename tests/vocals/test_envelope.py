"""Regression tests for the per-hop RMS envelope sidecar.

The envelope exists so that every future choice of ON_RATIO, OFF_RATIO,
MERGE_GAP_S, MIN_REGION_S or an absolute level floor is a local re-derivation
rather than a GPU re-separation of the library. That only holds if the stored
arrays reproduce the regions the farm actually published, so these tests check
round-trip fidelity against the real region maths, not just byte equality.

Single-line intent, in the repo's regression style:
  - if a written envelope does not round-trip then the stored input is not the one used
  - if float16 flips a threshold decision then re-derived regions disagree with published ones
  - if a missing sidecar raises then every pre-envelope entry becomes an error
  - if a drifted signature is served then stale envelopes silently poison analysis
  - if a truncated file is padded then corruption reads as data
  - if the envelope lands inside the cache entry then every listing row pays for it
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("modal", reason="needs the optional modal package")

from apps.vocals.envelope import (
    ENVELOPE_SCHEMA,
    envelope_path,
    read_envelope,
    write_envelope,
)
from scripts.modal_vocal_farm import (
    HOP_S,
    OFF_RATIO,
    ON_RATIO,
    ratio_envelope,
    regions_from_envelope,
)

SIGNATURE: dict[str, int] = {
    "device": 1, "inode": 2, "size": 3, "mtime_ns": 4,
}


def _f16(values: list[float]) -> bytes:
    return np.asarray(values, dtype="<f2").tobytes()


def _write(tmp_path: Path, v: list[float], m: list[float], **kwargs) -> Path:
    path = envelope_path(tmp_path, "abc123")
    return write_envelope(
        path,
        stable_id=kwargs.get("stable_id", "abc123"),
        hop_s=HOP_S,
        frames=len(v),
        vocals_rms_bytes=_f16(v),
        mix_rms_bytes=_f16(m),
        audio_signature=kwargs.get("audio_signature", SIGNATURE),
    )


def test_round_trips(tmp_path: Path) -> None:
    """if a written envelope does not round-trip then the stored input is wrong"""
    v = [0.0, 0.01, 0.5, 0.25, 0.125]
    m = [0.001, 0.1, 1.0, 0.5, 0.25]
    got = read_envelope(_write(tmp_path, v, m))
    assert got is not None
    assert got.frames == 5
    assert got.hop_s == HOP_S
    assert np.allclose(got.vocals_rms, np.asarray(v, dtype="float16"), atol=1e-7)
    assert np.allclose(got.mix_rms, np.asarray(m, dtype="float16"), atol=1e-7)


def test_arrays_do_not_get_swapped(tmp_path: Path) -> None:
    """if v and m are swapped then every ratio is inverted"""
    got = read_envelope(_write(tmp_path, [0.1] * 4, [0.9] * 4))
    assert got is not None
    assert got.vocals_rms.tolist() == pytest.approx([0.1] * 4, abs=1e-3)
    assert got.mix_rms.tolist() == pytest.approx([0.9] * 4, abs=1e-3)


def test_float16_does_not_flip_a_threshold(tmp_path: Path) -> None:
    """if float16 flips a threshold then re-derived regions disagree with published"""
    rng = np.random.default_rng(0)
    frames = 400
    mix = rng.uniform(0.01, 1.0, frames)
    # Ratios deliberately clustered around the ON/OFF thresholds, which is
    # where a precision loss would actually change a decision.
    ratios = rng.uniform(OFF_RATIO * 0.5, ON_RATIO * 2.0, frames)
    vocals = mix * ratios

    exact = regions_from_envelope(
        ratio_envelope(vocals.tolist(), mix.tolist(), HOP_S), frames * HOP_S
    )
    stored = read_envelope(_write(tmp_path, vocals.tolist(), mix.tolist()))
    assert stored is not None
    rebuilt = regions_from_envelope(
        ratio_envelope(
            stored.vocals_rms.tolist(), stored.mix_rms.tolist(), HOP_S
        ),
        frames * HOP_S,
    )
    assert rebuilt == exact, "float16 storage changed the derived regions"


def test_absent_sidecar_is_none_not_an_error(tmp_path: Path) -> None:
    """if a missing sidecar raises then every pre-envelope entry becomes an error"""
    assert read_envelope(envelope_path(tmp_path, "never-written")) is None


def test_signature_drift_is_refused(tmp_path: Path) -> None:
    """if a drifted signature is served then stale envelopes poison analysis"""
    path = _write(tmp_path, [0.1, 0.2], [0.3, 0.4])
    other = dict(SIGNATURE, inode=999)
    with pytest.raises(ValueError, match="disagrees with the cache entry"):
        read_envelope(path, expect_signature=other)
    # The matching signature still reads fine.
    assert read_envelope(path, expect_signature=SIGNATURE) is not None


def test_truncated_body_raises(tmp_path: Path) -> None:
    """if a truncated file is padded then corruption reads as data"""
    path = _write(tmp_path, [0.1, 0.2, 0.3], [0.4, 0.5, 0.6])
    raw = path.read_bytes()
    path.write_bytes(raw[:-4])
    with pytest.raises(ValueError, match="body is"):
        read_envelope(path)


def test_frame_count_must_match_bytes(tmp_path: Path) -> None:
    """if frames and bytes disagree then the header lies about the payload"""
    with pytest.raises(ValueError, match="expected"):
        write_envelope(
            envelope_path(tmp_path, "x"),
            stable_id="x",
            hop_s=HOP_S,
            frames=10,
            vocals_rms_bytes=_f16([0.1, 0.2]),
            mix_rms_bytes=_f16([0.1, 0.2]),
            audio_signature=SIGNATURE,
        )


def test_header_is_readable_without_numpy(tmp_path: Path) -> None:
    """if the header is not plain JSON then the format is not self-describing"""
    path = _write(tmp_path, [0.1, 0.2], [0.3, 0.4])
    first_line = path.read_bytes().split(b"\n", 1)[0]
    header = json.loads(first_line)
    assert header["schema"] == ENVELOPE_SCHEMA
    assert header["dtype"] == "float16"
    assert header["order"] == ["vocals_rms", "mix_rms"]
    assert header["audio_signature"] == SIGNATURE


def test_envelope_never_lands_in_the_cache_entry(tmp_path: Path) -> None:
    """if the envelope lands in the entry then every listing row pays for it"""
    from apps.vocals import cache as vcache
    from scripts.modal_vocal_farm import (
        PRESETS,
        GapTrack,
        _signature_of,
        _write_result,
    )

    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"not really audio")
    # The REAL signature: write_entry refuses to publish if it disagrees with
    # the file on disk, which is the iCloud-inode guard doing its job.
    signature = _signature_of(audio.stat())
    frames = 6
    result = {
        "schema": 2,
        "source": "demucs-htdemucs",
        "fps": 1.0 / HOP_S,
        "duration_s": 180.0,
        "coverage_pct": 42.0,
        "regions": [{"start_s": 1.0, "end_s": 9.0, "confidence": 0.8}],
        "device": "cuda",
        "source_sample_rate": 44100,
        "analysis_sample_rate": 44100,
        "wall_span": [1000.0, 1008.1],
        "container_s": 8.1,
        "timings": {"load_s": 0.4, "separate_s": 5.6},
        "envelope": {
            "hop_s": HOP_S,
            "frames": frames,
            "dtype": "float16",
            "vocals_rms": _f16([0.1] * frames),
            "mix_rms": _f16([0.9] * frames),
        },
    }
    track = GapTrack(stable_id="sid1", audio_path=audio)
    data_dir = tmp_path / "data"
    _write_result(
        data_dir, track, result, PRESETS["htdemucs-ov0.1"], signature, 123.0
    )

    entry = json.loads(vcache.cache_path(data_dir, "sid1").read_text())
    assert "envelope" not in entry
    assert "envelope" not in entry["worker"]
    stored = read_envelope(
        envelope_path(vcache.cache_dir(data_dir), "sid1"),
        expect_signature=signature,
    )
    assert stored is not None
    assert stored.frames == frames
