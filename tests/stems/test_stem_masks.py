"""STEMSTORE-01: stems stored as the source plus compact masks and coded low parts."""

from __future__ import annotations

import numpy as np
import pytest

from apps.stems import stem_masks as sm

# soundfile + scipy come with the analysis extra; tests/conftest.py reports them UNAVAILABLE, never silently.
pytestmark = pytest.mark.requires_audio_stack
RATE = 44100


def _butter_sos(fc: float | list[float], btype: str) -> np.ndarray:
    from scipy.signal import butter

    return butter(8, fc, btype=btype, fs=RATE, output="sos")


def sosfiltfilt(sos: np.ndarray, x: np.ndarray, axis: int = -1) -> np.ndarray:
    from scipy.signal import sosfiltfilt as _sosfiltfilt

    return _sosfiltfilt(sos, x, axis=axis)


def _band_noise(rng: np.random.Generator, lo: float, hi: float, sec: float = 3.0) -> np.ndarray:
    sos = _butter_sos([lo, hi], "band")
    return (sosfiltfilt(sos, rng.standard_normal((int(sec * RATE), 2)), axis=0) * 0.3).astype(np.float32)


def _depth(ref: np.ndarray, got: np.ndarray) -> float:
    return sm._depth(ref, got)


@pytest.fixture(scope="module")
def disjoint() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(7)
    t = np.arange(int(3.0 * RATE)) / RATE
    bass = np.stack([0.4 * np.sin(2 * np.pi * 55 * t)] * 2, axis=1).astype(np.float32)
    return {
        "vocals": _band_noise(rng, 2000, 4000),
        "drums": _band_noise(rng, 7000, 11000),
        "bass": bass,
        "other": _band_noise(rng, 600, 1200),
    }


def test_if_the_reconstructed_parts_do_not_sum_to_the_source_then_broken(disjoint: dict[str, np.ndarray]) -> None:
    source = sum(disjoint.values())
    blob = sm.encode_masks(source, disjoint, RATE)
    parts = sm.reconstruct_parts(source, RATE, blob)
    assert _depth(source, sum(parts.values())) > 60, (
        "if the parts reconstructed from the source do not sum back to it then broken"
    )
    masks_blob, low_blobs = sm.encode_bundle(source, disjoint, RATE)
    lows = {s: sm.decode_low_part(b, RATE, len(source)) for s, b in low_blobs.items()}
    with_lows = sm.reconstruct_parts(source, RATE, masks_blob, lows)
    assert _depth(source, sum(with_lows.values())) > 60, "if the coded low parts break the exact sum then broken"
    with pytest.raises(sm.MaskFormatError, match="low parts"):
        sm.reconstruct_parts(source, RATE, masks_blob)  # the header names low parts; none given


def test_if_a_coded_stem_loses_its_own_low_band_then_broken(disjoint: dict[str, np.ndarray]) -> None:
    source = sum(disjoint.values())
    masks_blob, low_blobs = sm.encode_bundle(source, disjoint, RATE)
    lows = {s: sm.decode_low_part(b, RATE, len(source)) for s, b in low_blobs.items()}
    parts = sm.reconstruct_parts(source, RATE, masks_blob, lows)
    edge = RATE // 10
    got = _depth(disjoint["bass"][edge:-edge], parts["bass"][edge:-edge])
    assert got > 20, f"if the bass (coded low part plus its residual mask) reconstructs at {got:.1f} dB then broken"


def test_if_masks_do_not_separate_disjoint_stems_then_broken(disjoint: dict[str, np.ndarray]) -> None:
    source = sum(disjoint.values())
    parts = sm.reconstruct_parts(source, RATE, sm.encode_masks(source, disjoint, RATE))
    edge = RATE // 10  # the STFT padding region at either end is not the claim
    for stem, truth in disjoint.items():
        got = _depth(truth[edge:-edge], parts[stem][edge:-edge])
        assert got > 20, f"if {stem} in its own band reconstructs at {got:.1f} dB (< 20) then broken"


def test_if_a_silent_stem_picks_up_the_mix_then_broken(disjoint: dict[str, np.ndarray]) -> None:
    parts_in = {**disjoint, "vocals": np.zeros_like(disjoint["vocals"])}
    source = sum(parts_in.values())
    parts = sm.reconstruct_parts(source, RATE, sm.encode_masks(source, parts_in, RATE))
    leak = 10 * np.log10(max(float(np.sum(parts["vocals"] ** 2)), 1e-30) / float(np.sum(source**2)))
    assert leak < -40, f"if a silent stem holds {leak:.1f} dB of the mix (> -40) then broken"


def test_if_a_bad_payload_or_wrong_source_is_applied_then_broken(disjoint: dict[str, np.ndarray]) -> None:
    source = sum(disjoint.values())
    blob = sm.encode_masks(source, disjoint, RATE)
    with pytest.raises(sm.MaskFormatError, match="magic"):
        sm.decode_masks(b"XXXX" + blob[4:])
    with pytest.raises(sm.MaskFormatError, match="version"):
        sm.decode_masks(blob[:4] + (99).to_bytes(2, "little") + blob[6:])
    with pytest.raises(sm.MaskFormatError, match="frames"):
        sm.reconstruct_parts(source[:-100], RATE, blob)
    tampered = blob.replace(b'"t_pool":2', b'"t_pool":4')
    with pytest.raises(sm.MaskFormatError, match="shape"):
        sm.decode_masks(tampered)
    with pytest.raises(sm.MaskFormatError, match="Hz"):
        sm.reconstruct_parts(source, 48000, blob)
    reordered = blob.replace(b'"derived":"other"', b'"derived":"bass!"')
    with pytest.raises(sm.MaskFormatError, match="derived"):
        sm.decode_masks(reordered)


def test_if_a_coded_low_part_is_shifted_then_broken() -> None:
    rng = np.random.default_rng(3)
    sos = _butter_sos(400, "low")
    # Near-mono like real kick and bass lows (side measured -12 to -25 dB under mid, round 2e):
    # uncorrelated stereo noise at 24 kbps drops to joint stereo and nulls only ~7 dB.
    mid = sosfiltfilt(sos, rng.standard_normal(3 * RATE)) * 0.3
    side = sosfiltfilt(sos, rng.standard_normal(3 * RATE)) * 0.03
    part = np.stack([mid + side, mid - side], axis=1).astype(np.float32)
    got = sm.decode_low_part(sm.encode_low_part(part, RATE), RATE, len(part))
    x, y = part[:, 0].astype(np.float64), got[:, 0].astype(np.float64)
    # y[n] ~ x[n - lag]; parabolic peak interpolation gives the sub-sample lag
    score = np.array([np.dot(y[RATE : 2 * RATE], x[RATE - lag : 2 * RATE - lag]) for lag in range(-8, 9)])
    i = int(np.argmax(score))
    a, b, c = score[i - 1], score[i], score[i + 1]
    lag = i - 8 + 0.5 * (a - c) / (a - 2 * b + c)
    # measured Wed 4 Nov 2026: -0.68 samples at 400 Hz (Opus phase response, under 3 degrees at 500 Hz)
    assert abs(lag) < 1.0, f"if the Opus low part lands {lag:.2f} samples off (>= 1) then broken"
    assert _depth(part[RATE:-RATE], got[RATE:-RATE]) > 10, "if the coded low band is not waveform-faithful then broken"
