"""Measure loop seam clicks and reverse playback from probe-loop-seam-click renders (spike, issue #9843).

Reads the manifest written by ``apps/webui/frontend/scripts/probe-loop-seam-click.mjs`` and prints the
Markdown tables that ``(private path)`` records.

Usage, from the repo root::

    uv run python -m scripts.quality.loop_seam .tmp/loop-seam/manifest.json

Requirements:
- ✔︎ ✅ 🎯 Every PCM file is checked against its manifest sha256 before it is measured.
    [if] a render is edited after the probe wrote it [then ⛔️] the loader raises.
- ✔︎ ✅ 🎯 The output offset is found by correlation against the source, never from node latency.
    [if] the output is the source delayed by 117 frames [then ⛔️] the finder returns anything but 117 +- 1.
    [if] the normalized correlation peak is under 0.5 [then ⛔️] the row reads UNKNOWN instead of a number.
- ✔︎ ✅ 🎯 Seams are compared with two controls: mid-loop points of the same render (floor) and a numpy
  hard splice (what a loop with no stretcher and no crossfade does).
    [if] a sine cut mid-cycle is spliced [then ⛔️] the splice median is not over 1.5.
    [if] a continuous sine is measured at the same points [then ⛔️] the ratio is not under 1.5.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from apps.transition_score.http import click_ratio, mono

CLICK_VETO = 1.5
OFFSET_SEARCH_S = 0.3
OFFSET_MIN_CORRELATION = 0.5
WRAP_TAIL_GUARD_S = 0.15
BAKED_ENGAGE_GUARD_S = 0.05
REVERSE_CHECK_S = 1.0
REVERSE_SEGMENT_S = 0.3
REVERSE_PROBE_POINTS_S = (0.3, 1.0, 1.5, 2.5)
REVERSE_TRACK_TOLERANCE_S = 0.05
REVERSED_CORRELATION = 0.8
SILENT_PEAK_DBFS = -60.0


@dataclass
class SeamRow:
    variant: str
    kind: str
    loop_ms: float
    ratios: list[float]
    note: str = ""


# ----------------------------------------------------------------- loading


def load_verified_pcm(path: Path, expected_sha256: str, channels: int) -> np.ndarray:
    """Float32 PCM, shape (frames, channels) or (frames,), refused on a sha256 mismatch."""
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"{path.name}: sha256 {actual} does not match the manifest {expected_sha256}")
    pcm = np.frombuffer(raw, dtype="<f4").astype(np.float64)
    return pcm if channels == 1 else pcm.reshape(-1, channels)


# ----------------------------------------------------------------- instruments


def _normalized_xcorr(signal: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Normalized correlation of ``template`` at every valid offset into ``signal`` (FFT)."""
    n, m = len(signal), len(template)
    size = 1 << (n + m).bit_length()
    raw = np.fft.irfft(np.fft.rfft(signal, size) * np.conj(np.fft.rfft(template, size)), size)[: n - m + 1]
    cumulative = np.concatenate(([0.0], np.cumsum(signal**2)))
    energy = cumulative[m:] - cumulative[: n - m + 1]
    return raw / (np.sqrt(energy * float(np.sum(template**2))) + 1e-12)


def find_output_offset_frames(
    output: np.ndarray, source: np.ndarray, source_start: int, window: tuple[int, int], max_lag: int
) -> tuple[int, float]:
    """Lag d (frames) and peak with output[t] ~= source[source_start + t - d], searched over 0..max_lag."""
    lo, hi = window
    template = mono(output)[lo:hi]
    search_lo = source_start + lo - max_lag
    if search_lo < 0:
        raise ValueError("offset search runs before the start of the source")
    corr = _normalized_xcorr(source[search_lo : source_start + hi], template)
    best = int(np.argmax(corr))
    return max_lag - best, float(corr[best])


def splice_join_ratios(source: np.ndarray, start: int, length: int, repeats: int, sr: int) -> list[float]:
    """click_ratio at each join of a hard numpy tile of source[start:start+length] (no fade, no stretch)."""
    tiled = np.tile(source[start : start + length], repeats)
    joins = [k * length / sr for k in range(1, repeats)]
    edge = 0.12
    return [click_ratio(tiled, sr, j) for j in joins if edge <= j <= len(tiled) / sr - edge]


def _summary(ratios: list[float]) -> str:
    if not ratios:
        return "0 | n/a | n/a | n/a | n/a"
    arr = np.array(ratios)
    over = float(np.mean(arr > CLICK_VETO))
    return f"{len(arr)} | {np.median(arr):.2f} | {np.percentile(arr, 90):.2f} | {arr.max():.2f} | {over:.0%}"


def _dbfs(x: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(x**2)))
    return -np.inf if rms <= 0 else 20 * np.log10(rms)


# ----------------------------------------------------------------- per-variant measurement


def seam_rows(variant: dict, out: np.ndarray, source: np.ndarray, m: dict, offset_s: float) -> list[SeamRow]:
    sr, loop_s = m["sample_rate"], variant["loop_s"]
    engage = m["engage_s"] + offset_s
    wraps = []
    k = 1
    while engage + k * loop_s < m["render_s"] - WRAP_TAIL_GUARD_S:
        w = engage + k * loop_s
        if not (variant["kind"] == "baked" and w < engage + BAKED_ENGAGE_GUARD_S):
            wraps.append(w)
        k += 1
    loop_ms = loop_s * 1000
    rows = [SeamRow(variant["id"], variant["kind"], loop_ms, [click_ratio(out, sr, w) for w in wraps])]
    if variant["kind"] == "today":
        if variant["beats"] >= 1 / 8:
            mids = [w + loop_s / 2 for w in wraps if w + loop_s / 2 < m["render_s"] - WRAP_TAIL_GUARD_S]
            rows.append(
                SeamRow(f"floor-{variant['id'][6:]}", "control", loop_ms, [click_ratio(out, sr, x) for x in mids])
            )
        else:
            half = loop_s / 2
            rows.append(
                SeamRow(
                    f"{variant['id']} (half window L/2, non-house)",
                    "today",
                    loop_ms,
                    [click_ratio(out, sr, w, half_window_s=half) for w in wraps],
                    "non-house window",
                )
            )
        start, length = round(m["loop_start_s"] * sr), round(loop_s * sr)
        repeats = max(3, int(4.0 / loop_s))
        rows.append(
            SeamRow(
                f"splice-{variant['id'][6:]}",
                "control",
                loop_ms,
                splice_join_ratios(source, start, length, repeats, sr),
            )
        )
    return rows


def _peak_near(haystack: np.ndarray, seg: np.ndarray, start: int, slack: int) -> float:
    """Best normalized correlation of ``seg`` placed at ``start`` +- ``slack`` frames in ``haystack``."""
    lo = max(0, start - slack)
    return float(_normalized_xcorr(haystack[lo : start + len(seg) + slack], seg).max())


def reverse_row(variant: dict, out: np.ndarray, source: np.ndarray, m: dict, offset_s: float) -> str:
    """Does each short output segment after engage match the source read backwards from S + 2 s?

    The fixture is a periodic drum loop, so a global search locks onto a repeat a bar away; the
    match is taken only within REVERSE_TRACK_TOLERANCE_S of where reversed play must be.
    """
    sr = m["sample_rate"]
    engage_s = m["engage_s"] + offset_s
    seg_frames = round(REVERSE_SEGMENT_S * sr)
    slack = round(REVERSE_TRACK_TOLERANCE_S * sr)
    pivot = round((m["loop_start_s"] + 2.0) * sr)
    reversed_source = source[::-1]
    speed = abs(variant["rate"])
    rev_peaks, fwd_peaks = [], []
    for after in REVERSE_PROBE_POINTS_S:
        start = round((engage_s + after) * sr)
        seg = mono(out)[start : start + seg_frames]
        # reversed_source[i] = source[len - 1 - i]; reversed play from pivot reaches pivot - after * speed.
        rev_peaks.append(_peak_near(reversed_source, seg, len(source) - pivot + round(after * speed * sr), slack))
        fwd_peaks.append(_peak_near(source, seg, pivot - round(after * speed * sr) - seg_frames, slack))
    engage_seg = mono(out)[round(engage_s * sr) : round((engage_s + REVERSE_CHECK_S) * sr)]
    verdict = "plays reversed" if float(np.median(rev_peaks)) > REVERSED_CORRELATION else "not reversed"
    return (
        f"| {variant['id']} | {variant['rate']} | {', '.join(f'{c:.2f}' for c in rev_peaks)} | "
        f"{', '.join(f'{c:.2f}' for c in fwd_peaks)} | {_dbfs(engage_seg):.1f} | "
        f"{click_ratio(out, sr, engage_s):.2f} | {verdict} |"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: python -m scripts.quality.loop_seam <manifest.json>")
    manifest_path = Path(argv[0])
    m = json.loads(manifest_path.read_text())
    root, sr = manifest_path.parent, m["sample_rate"]
    source = load_verified_pcm(root / m["source_file"], m["source_sha256"], 1)
    lead_in = round((m["loop_start_s"] - m["engage_s"]) * sr)
    print(f"fixture {m['fixture']} sha256 {m['fixture_sha256']}")
    print(f"t0 {m['t0_s']:.4f} s, loop start S {m['loop_start_s']:.4f} s, beat {m['beat_s']:.6f} s")
    print(f"chromium {m['chromium_version']}, signalsmith-stretch {m['signalsmith_version']}")
    print(f"node.latency() {m['node_latency_s'] * 1000:.2f} ms (metadata only, not used to trim)\n")

    seam: list[SeamRow] = []
    reverse: list[str] = []
    for variant in m["variants"]:
        out = load_verified_pcm(root / variant["file"], variant["sha256"], 2)
        if variant["error"] is not None:
            reverse.append(f"| {variant['id']} | {variant['rate']} | n/a | n/a | n/a | n/a | {variant['error']} |")
            continue
        peak = float(np.abs(out).max())
        if peak <= 0 or 20 * np.log10(peak) < SILENT_PEAK_DBFS:
            raise ValueError(f"{variant['id']}: render is silent (peak under {SILENT_PEAK_DBFS} dBFS)")
        lag, peak_corr = find_output_offset_frames(
            out, source, lead_in, (round(0.1 * sr), round(0.9 * sr)), round(OFFSET_SEARCH_S * sr)
        )
        offset_s = lag / sr
        if peak_corr < OFFSET_MIN_CORRELATION:
            seam.append(SeamRow(variant["id"], variant["kind"], variant["loop_s"] * 1000, [], "UNKNOWN: offset"))
            continue
        if variant["kind"] in ("today", "baked"):
            rows = seam_rows(variant, out, source, m, offset_s)
            rows[0].note = f"offset {offset_s * 1000:.2f} ms, corr {peak_corr:.3f}"
            seam.extend(rows)
        elif variant["kind"] in ("rate-neg", "reversed"):
            reverse.append(reverse_row(variant, out, source, m, offset_s))

    print(f"| variant | kind | loop ms | wraps | median | p90 | max | share over {CLICK_VETO} | note |")
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for row in seam:
        print(f"| {row.variant} | {row.kind} | {row.loop_ms:.1f} | {_summary(row.ratios)} | {row.note} |")
    print(
        "\n| variant | rate | corr reversed at 0.3/1/1.5/2.5 s | corr forward same spans"
        " | RMS dBFS first 1 s | engage click | verdict |"
    )
    print("| --- | ---: | --- | --- | ---: | ---: | --- |")
    print("\n".join(reverse))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
