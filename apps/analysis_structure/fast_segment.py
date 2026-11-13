"""Own-fast sections: phrase boundaries from cached waveform peaks, no model.

WHY. All-In-One (``allin1_runner.py``) takes about 136 s a track on a 4-core
CPU, so most of a library has no sections until a batch job reaches it. This
module gives every track sections from data the app already holds for it: the
3-band waveform peaks (``apps.analysis_waveform``, 150 columns a second, low
under 200 Hz, mid, high over 4 kHz) and the downbeats of the track's own
beatgrid. Nothing is decoded and nothing is downloaded, so it runs in
milliseconds once those two exist. All-In-One stays the better source and
replaces these sections when its sidecar arrives.

THE METHOD (published, no learned weights). Each bar becomes one feature
vector: the mean level of each band over the bar and over each of its beats,
in dB, so both how loud each band is and its rhythm inside the bar count. The
vectors are standardized across the track, a bar-by-bar self-similarity
matrix is built from them, and a Gaussian-tapered checkerboard kernel slid
down its diagonal gives a novelty curve (Foote 2000, "Automatic audio
segmentation using a measure of audio novelty"). Boundaries are novelty peaks,
weighted toward phrase starts: a bar on the voted 8-bar phrase grid counts
more than one on the 4-bar grid, which counts more than any other bar. A DJ
track is built in 8, 16 and 32-bar blocks, so the grid is the strongest prior
there is.

LABELS. The sections are labeled by the same deterministic rules as All-In-One
sections (``dj_labels.py``), fed band envelopes instead of stem envelopes:
``bass`` is the low band, ``drums`` the high band (hats, claps, rides), and
``mix`` the sum of the three. These are proxies, and the bench names how far
they agree with the stem envelopes.

WHAT IS NEVER GUESSED. Fewer than ``MIN_BARS`` bars, or peaks that do
not cover the grid, return ``status: failed`` with the reason, never sections.
When ``dj_labels`` reports ``no_energy_contrast``, the track abstains: no
sections and ``abstain_reason`` set, never one unlabeled span.

-Claude
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise
from typing import Any

import numpy as np

from .dj_labels import label_sections, own_fast_abstain_reason
from .drop_events import mark_drops

PRODUCER = "own-fast"
PRODUCER_VERSION = "0.4.0"  # bump when the features, kernel, peak picking or proxies change

COLUMNS_PER_S = 150.0
BEATS_PER_BAR = 4
MIN_BARS = 16  # bars (the last one ends at the file end); under this there is no structure to find
KERNEL_HALF_BARS = 8  # checkerboard half-width: one 8-bar block either side
KERNEL_TAPER = 0.5  # Gaussian sigma as a fraction of the half-width
MIN_SECTION_BARS = 8
PEAK_WINDOW_BARS = 4  # a peak must be the largest within this many bars
THRESHOLD_SIGMA = 0.5  # keep peaks above mean + this many standard deviations
NOVELTY_MIN = 0.1  # and above this absolute novelty (see ``novelty``)
SIMILAR_DB = 3.0  # bars whose features differ by this much RMS are e^-1 similar
PHRASE_WEIGHT = {8: 1.0, 4: 0.75}  # novelty multiplier by grid; any other bar: OFF_GRID
OFF_GRID = 0.35
ENVELOPE_HOP_S = 0.1
FLOOR_DB = -60.0


def _band_db(peaks: np.ndarray) -> np.ndarray:
    """Peak bytes (0-255, linear) to dB re full scale, floored."""
    amp = np.maximum(peaks.astype(np.float64), 1.0) / 255.0
    return np.maximum(20.0 * np.log10(amp), FLOOR_DB)


def _mean_db(block: np.ndarray) -> np.ndarray:
    """Mean POWER of each column of ``block`` (dB in), back in dB."""
    if block.shape[0] == 0:
        return np.full(block.shape[1], FLOOR_DB)
    return 10.0 * np.log10(np.mean(10.0 ** (block / 10.0), axis=0))


def bar_edges(grid: Sequence[float], duration_s: float) -> list[float]:
    """Bar boundary times: every downbeat, plus the end of the last bar.

    A track with N downbeats has N bars when its last bar is there in full:
    that bar ends one median bar after the last downbeat, or at the end of the
    file if sooner. A last bar shorter than half a median bar is a tail, not a
    bar, and is left out so its few columns cannot read as a section change.
    """
    edges = list(grid)
    if len(edges) < 2:
        return edges
    bar_s = float(np.median(np.diff(edges)))
    end = min(duration_s, edges[-1] + bar_s)
    if end - edges[-1] >= bar_s / 2:
        edges.append(end)
    return edges


def bar_features(peaks: np.ndarray, downbeats: Sequence[float]) -> np.ndarray:
    """One row per bar: 3 band means, then 3 bands x 4 beat means, in dB."""
    db = _band_db(peaks)
    cols = np.asarray(downbeats, dtype=np.float64) * COLUMNS_PER_S
    rows = []
    for a, b in pairwise(cols):
        lo, hi = round(float(a)), round(float(b))
        bar = db[lo:hi]
        edges = np.linspace(lo, hi, BEATS_PER_BAR + 1).round().astype(int)
        beats = [_mean_db(db[edges[k] : edges[k + 1]]) for k in range(BEATS_PER_BAR)]
        rows.append(np.concatenate([_mean_db(bar), *beats]))
    return np.asarray(rows)


def self_similarity(features: np.ndarray) -> np.ndarray:
    """Bar-by-bar similarity in [0, 1] on an ABSOLUTE dB scale.

    exp(-mean squared dB difference / SIMILAR_DB^2). Absolute on purpose: a
    per-track scale (standardizing, or dividing by the median distance) turns
    the 1 dB wobble of a flat track into the same contrast as a drop, and the
    flat-track control then finds a dozen boundaries in noise.
    """
    sq = np.sum(features**2, axis=1)
    d2 = np.maximum(sq[:, None] + sq[None, :] - 2.0 * features @ features.T, 0.0)
    return np.exp(-(d2 / features.shape[1]) / SIMILAR_DB**2)


def _kernel(half: int) -> np.ndarray:
    idx = np.arange(-half, half) + 0.5
    sign = np.sign(idx)
    taper = np.exp(-((idx / (KERNEL_TAPER * half)) ** 2))
    return np.outer(sign, sign) * np.outer(taper, taper)


def novelty(ssm: np.ndarray, half: int = KERNEL_HALF_BARS) -> np.ndarray:
    """Foote checkerboard novelty at each bar START (index i = between bar i-1 and i).

    Scaled so 1.0 is two blocks that are each self-identical and wholly unlike
    each other, which makes ``NOVELTY_MIN`` an absolute floor. The matrix is
    padded by reflection so the first and last blocks get a full kernel
    instead of a zero tail that would read as a boundary.
    """
    n = ssm.shape[0]
    k = _kernel(half)
    k = k / np.abs(k).sum() * 2.0
    padded = np.pad(ssm, half, mode="reflect")
    out = np.zeros(n)
    for i in range(n):
        out[i] = float(np.sum(k * padded[i : i + 2 * half, i : i + 2 * half]))
    out[0] = 0.0
    return np.maximum(out, 0.0)


def vote_phrase_offset(curve: np.ndarray, phrase: int = 8) -> int:
    """The bar residue mod ``phrase`` that carries the most novelty."""
    sums = [float(curve[r::phrase].sum()) for r in range(phrase)]
    return int(np.argmax(sums))


def _weights(n: int, offset: int) -> np.ndarray:
    w = np.full(n, OFF_GRID)
    for grid in sorted(PHRASE_WEIGHT, reverse=True):
        for i in range(n):
            if (i - offset) % grid == 0 and w[i] < PHRASE_WEIGHT[grid]:
                w[i] = PHRASE_WEIGHT[grid]
    return w


def pick_boundaries(
    curve: np.ndarray,
) -> tuple[list[int], int, dict[int, float], list[dict[str, Any]]]:
    """Bar indices where a new section starts (bar 0 excluded), phrase offset, confidences, suppressed."""
    n = len(curve)
    offset = vote_phrase_offset(curve)
    score = curve * _weights(n, offset)
    live = score[1:]
    relative = float(live.mean() + THRESHOLD_SIGMA * live.std()) if len(live) else math.inf
    threshold = max(relative, NOVELTY_MIN)
    suppressed: list[dict[str, Any]] = []
    cands: list[tuple[int, float]] = []
    for i in range(1, n):
        lo, hi = max(1, i - PEAK_WINDOW_BARS), min(n, i + PEAK_WINDOW_BARS + 1)
        if score[i] != score[lo:hi].max():
            continue
        conf = float(score[i])
        if conf < threshold:
            suppressed.append({"start_bar": i, "confidence": conf})
            continue
        cands.append((i, conf))
    chosen: list[int] = []
    confidence: dict[int, float] = {}
    for i, conf in sorted(cands, key=lambda item: -item[1]):
        if i < MIN_SECTION_BARS or n - i < MIN_SECTION_BARS // 2:
            continue
        if all(abs(i - c) >= MIN_SECTION_BARS for c in chosen):
            chosen.append(i)
            confidence[i] = conf
    suppressed.sort(key=lambda row: row["start_bar"])
    return sorted(chosen), offset, confidence, suppressed


def band_envelopes(peaks: np.ndarray, hop_s: float = ENVELOPE_HOP_S) -> dict[str, Any]:
    """The ``dj_labels`` envelope contract from band peaks: bass=low, drums=high, mix=sum."""
    per_hop = max(1, round(hop_s * COLUMNS_PER_S))
    n = peaks.shape[0] // per_hop
    if n == 0:
        return {"hop_s": hop_s, "drums": [], "bass": [], "mix": []}
    power = 10.0 ** (_band_db(peaks[: n * per_hop]) / 10.0)
    hop_power = power.reshape(n, per_hop, 3).mean(axis=1)
    to_db = lambda p: [round(float(v), 2) for v in 10.0 * np.log10(np.maximum(p, 1e-12))]  # noqa: E731
    return {
        "hop_s": hop_s,
        "bass": to_db(hop_power[:, 0]),
        "drums": to_db(hop_power[:, 2]),
        "mix": to_db(hop_power.sum(axis=1)),
    }


def _grid_outruns_peaks(downbeats: Sequence[float], peaks: np.ndarray) -> bool:
    """Whether the UNFILTERED grid's last downbeat lies more than one beat past the peaks' end.

    A whole track's last downbeat starts its last bar, so it sits at or before
    the end of the audio; one beat of tolerance covers decode rounding.

    Checked before ``segment`` trims the grid to ``duration_s``: the caller
    passes the peaks' own length as the duration, so a truncated peaks array
    would otherwise just lose its tail bars and pass as a whole track.
    """
    times = sorted(float(t) for t in downbeats)
    if len(times) < 2:
        return False
    bar_s = float(np.median(np.diff(times)))
    return times[-1] > peaks.shape[0] / COLUMNS_PER_S + bar_s / 4


def _sections_from_bars(
    grid: list[float],
    bars: list[int],
    duration_s: float,
    boundary_confidence: dict[int, float] | None = None,
) -> list[dict[str, Any]]:
    """One section per boundary bar, each starting on a downbeat the deck draws.

    The first section starts on the first downbeat too: a pickup before it is
    left outside the sections.
    """
    bar_starts = [0, *bars]
    starts = [grid[b] for b in bar_starts]
    ends = [*starts[1:], duration_s]
    bar_ends: list[int | None] = [*bars, None]
    conf = boundary_confidence or {}
    sections: list[dict[str, Any]] = []
    for s, e, sb, be in zip(starts, ends, bar_starts, bar_ends, strict=True):
        row: dict[str, Any] = {
            "start_s": round(s, 3),
            "end_s": round(e, 3),
            "label": "section",
            "start_bar": sb,
            "bars": (be - sb) if be is not None else None,
        }
        if sb > 0 and sb in conf:
            row["confidence"] = conf[sb]
        sections.append(row)
    return sections


def _apply_dj_labels(sections: list[dict[str, Any]], dj: dict[str, Any]) -> list[dict[str, Any]]:
    """Copy the DJ labels onto ``sections`` in place; return the drop moments."""
    if dj["status"] != "ok":
        return []
    for sec, lab in zip(sections, dj["sections"], strict=True):
        sec["dj_label"] = lab["dj_label"]
        sec["ambient"] = lab["ambient"]
        sec["energy"] = lab["energy"]
    return mark_drops(sections)


def segment(peaks: np.ndarray, downbeats: Sequence[float], duration_s: float) -> dict[str, Any]:
    """Sections for one track, PSSI-shaped like the All-In-One sidecar's.

    Returns ``{"status": "ok", "sections": [...], "dj_labels": {...}, ...}``,
    ``{"status": "abstain", "abstain_reason": ..., "sections": []}``, or
    ``{"status": "failed", "reason": ...}``.
    """
    if peaks.ndim != 2 or peaks.shape[1] != 3:
        return {"status": "failed", "reason": f"bad_peaks_shape:{tuple(peaks.shape)}"}
    if _grid_outruns_peaks(downbeats, peaks):
        return {"status": "failed", "reason": "peaks_shorter_than_grid"}
    grid = [float(t) for t in downbeats if 0.0 <= float(t) < duration_s]
    edges = bar_edges(grid, duration_s)
    if len(edges) - 1 < MIN_BARS:
        return {"status": "failed", "reason": f"too_few_bars:{max(len(edges) - 1, 0)}"}
    features = bar_features(peaks, edges)
    curve = novelty(self_similarity(features))
    bars, offset, boundary_confidence, suppressed = pick_boundaries(curve)
    sections = _sections_from_bars(grid, bars, duration_s, boundary_confidence)
    dj = label_sections(sections, band_envelopes(peaks))
    abstain = own_fast_abstain_reason(dj)
    if abstain is not None:
        return {
            "status": "abstain",
            "abstain_reason": abstain,
            "producer": PRODUCER,
            "producer_version": PRODUCER_VERSION,
            "sections": [],
            "moments": [],
            "dj_labels": {k: v for k, v in dj.items() if k != "sections"},
        }
    moments = _apply_dj_labels(sections, dj)
    return {
        "status": "ok",
        "producer": PRODUCER,
        "producer_version": PRODUCER_VERSION,
        "sections": sections,
        "moments": moments,
        "phrase_offset": offset,
        "suppressed": suppressed,
        "dj_labels": {k: v for k, v in dj.items() if k != "sections"},
    }


def phrase_windows_ms(sections: list[dict[str, Any]]) -> list[dict[str, int]]:
    """Own-fast section bounds in milliseconds for segment enrichment claims."""
    from apps.genre_infer import segment_claims

    return [
        {"start_ms": window.start_ms, "end_ms": window.end_ms, "start_bar": window.start_bar}
        for window in segment_claims.phrase_windows_from_sections(sections)
    ]


__all__ = [
    "PRODUCER",
    "PRODUCER_VERSION",
    "band_envelopes",
    "bar_edges",
    "bar_features",
    "novelty",
    "pick_boundaries",
    "segment",
    "self_similarity",
    "phrase_windows_ms",
]
