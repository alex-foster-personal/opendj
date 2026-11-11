/**
 * Amplitude-to-height shaping shared by the wavestack band painters
 * (render.ts and wave-band-layers.ts). Rendering only: the band DATA is never
 * modified.
 */
import type { AnlzWaveform } from '$lib/rb/anlz-types';

/** Rendering style only (matches rekordbox's white core): highs are drawn
 * at reduced height so the white band reads as the inner core. The band
 * DATA is untouched. */
export const HIGH_BAND_SCALE = 0.6;
export const MID_BAND_SCALE = 0.85;

/** Perceptual amplitude shaping (rendering only, the band DATA is never
 * modified). Raw PWV6/PWV7 bytes sit mostly in the 0.3-0.7 range after
 * /127 scaling, which painted linearly reads as a thin ribbon in a 40px
 * row; rekordbox draws visibly denser. Two standard display steps:
 *   1. per-track per-band normalization to the band's own 99th percentile
 *      (loud parts of THIS track reach full height; relative dynamics -
 *      quiet intros vs drops - are preserved);
 *   2. gamma lift v^0.45 to widen the mid range.
 * Zero stays zero; nothing is drawn where the analysis is silent. */
export const AMP_GAMMA = 0.45;
/** Floor for the p99 divisor so near-silent bands cannot blow up noise. */
const NORM_FLOOR = 0.1;

export interface BandNorms {
	low: number;
	mid: number;
	high: number;
}

/** Per-waveform normalization cache - computed once per anlz payload. */
const _normCache = new WeakMap<AnlzWaveform, BandNorms>();

function _p99(values: number[]): number {
	const nonZero = values.filter((v) => v > 0);
	if (nonZero.length === 0) return 1;
	const sorted = nonZero.slice().sort((a, b) => a - b);
	const p99 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.99))];
	return Math.min(1, Math.max(NORM_FLOOR, p99));
}

export function normsFor(waveform: AnlzWaveform): BandNorms {
	const cached = _normCache.get(waveform);
	if (cached !== undefined) return cached;
	const bands = waveform.detail;
	const norms: BandNorms = {
		low: _p99(bands.low),
		mid: _p99(bands.mid),
		high: _p99(bands.high)
	};
	_normCache.set(waveform, norms);
	return norms;
}

export function ampOf(v: number, norm: number): number {
	if (v <= 0) return 0;
	return Math.pow(Math.min(1, v / norm), AMP_GAMMA);
}

/** WAVE-EQ-5230: height factor for a linear band gain. The gain scales the
 * band's DRAWN height linearly, after the AMP_GAMMA display curve, so a kill
 * (gain near 0) leaves the band near zero height and the row agrees with the
 * overview strip, which scales its bars by the same linear gain. Scaling the
 * amplitude before the curve instead would leave a 0.05 kill at ~26% height. */
export function bandHeightScale(gain: number): number {
	if (!Number.isFinite(gain) || gain < 0) {
		throw new RangeError(`band gain must be finite and >= 0, got ${gain}`);
	}
	return gain;
}

export function bucketMax(values: number[], p0: number, p1: number): number {
	let max = 0;
	for (let i = p0; i <= p1; i++) {
		const v = values[i];
		if (v > max) max = v;
	}
	return max;
}

export function mirrorRect(path: Path2D, x: number, centerY: number, halfHeight: number): void {
	path.rect(x, centerY - halfHeight, 1, halfHeight * 2);
}

/** WAVEQS-11733: one-sided bar rising from the bottom baseline. */
export function upRect(path: Path2D, x: number, baseY: number, height: number): void {
	path.rect(x, baseY - height, 1, height);
}
