/**
 * First-select headphone CUE latency: play a chirp on the cue sink, hear it
 * with the built-in mic, cross-correlate, fill Mixxx HEAD DELAY (0-500 ms).
 *
 * Pure DSP + policy. The Web Audio play/record shell lives in headphones.ts
 * so this module stays a leaf (no mixer state, no DOM).
 */

import { HEAD_DELAY_MAX_MS } from '$lib/player/constants';

/** Cosine-similarity peak below this is noise, not a click the mic heard. */
export const CUE_LATENCY_PEAK_MIN = 0.35;

export const CUE_LATENCY_CLICK_MS = 8;
export const CUE_LATENCY_PERIOD_MS = 60;
export const CUE_LATENCY_CLICK_COUNT = 4;
export const CUE_LATENCY_PREROLL_MS = 40;

/** Level calculated from audio samples received by the production capture
 * path.  This is an observation of that buffer, not an acoustic-output claim. */
export function capturedSignalLevel(samples: Float32Array): { rms: number; peak: number } {
	if (!(samples instanceof Float32Array)) throw new TypeError('captured signal must be a Float32Array');
	if (samples.length === 0) return { rms: 0, peak: 0 };
	let sumSquares = 0;
	let peak = 0;
	for (const sample of samples) {
		if (!Number.isFinite(sample)) continue;
		const magnitude = Math.abs(sample);
		peak = Math.max(peak, magnitude);
		sumSquares += sample * sample;
	}
	return { rms: Math.sqrt(sumSquares / samples.length), peak };
}

export function cueLatencyCaptureMs(maxLagMs = HEAD_DELAY_MAX_MS): number {
	if (!Number.isFinite(maxLagMs) || maxLagMs < 0) {
		throw new RangeError(`cue latency capture window must be >= 0, got ${maxLagMs}`);
	}
	return (
		CUE_LATENCY_PREROLL_MS +
		CUE_LATENCY_PERIOD_MS * CUE_LATENCY_CLICK_COUNT +
		maxLagMs +
		40
	);
}

/** Hann-windowed 500-2000 Hz chirp train. Unique enough that room noise fails. */
export function cueLatencyClickTrain(opts: {
	sampleRate: number;
	clickMs?: number;
	periodMs?: number;
	clickCount?: number;
}): Float32Array {
	const sampleRate = opts.sampleRate;
	if (!Number.isFinite(sampleRate) || sampleRate <= 0) {
		throw new RangeError(`cue latency sampleRate must be positive, got ${sampleRate}`);
	}
	const clickMs = opts.clickMs ?? CUE_LATENCY_CLICK_MS;
	const periodMs = opts.periodMs ?? CUE_LATENCY_PERIOD_MS;
	const clickCount = opts.clickCount ?? CUE_LATENCY_CLICK_COUNT;
	if (!Number.isInteger(clickCount) || clickCount < 1) {
		throw new RangeError(`cue latency clickCount must be a positive integer, got ${clickCount}`);
	}
	const clickSamples = Math.max(1, Math.round((clickMs / 1000) * sampleRate));
	const periodSamples = Math.max(clickSamples, Math.round((periodMs / 1000) * sampleRate));
	const total = periodSamples * (clickCount - 1) + clickSamples;
	const out = new Float32Array(total);
	for (let n = 0; n < clickCount; n += 1) {
		const origin = n * periodSamples;
		for (let i = 0; i < clickSamples; i += 1) {
			const frac = clickSamples === 1 ? 0 : i / (clickSamples - 1);
			const freq = 500 + 1500 * frac;
			const t = i / sampleRate;
			const hann = 0.5 * (1 - Math.cos((2 * Math.PI * i) / Math.max(1, clickSamples - 1)));
			out[origin + i] = Math.sin(2 * Math.PI * freq * t) * hann;
		}
	}
	return out;
}

export function crossCorrelateLagMs(
	reference: Float32Array,
	captured: Float32Array,
	sampleRate: number
): { lagMs: number; peakNormalized: number } {
	if (!Number.isFinite(sampleRate) || sampleRate <= 0) {
		throw new RangeError(`sampleRate must be positive, got ${sampleRate}`);
	}
	if (reference.length === 0) throw new Error('cue latency reference is empty');
	if (captured.length < reference.length) {
		throw new Error(
			`cue latency capture is shorter than the reference (${captured.length} < ${reference.length})`
		);
	}
	let refEnergy = 0;
	for (let i = 0; i < reference.length; i += 1) refEnergy += reference[i] * reference[i];
	if (refEnergy === 0) throw new Error('cue latency reference has no energy');
	const maxLag = captured.length - reference.length;
	let bestLag = 0;
	let best = -Infinity;
	for (let lag = 0; lag <= maxLag; lag += 1) {
		let dot = 0;
		let capEnergy = 0;
		for (let i = 0; i < reference.length; i += 1) {
			const c = captured[i + lag];
			dot += reference[i] * c;
			capEnergy += c * c;
		}
		const denom = Math.sqrt(refEnergy * capEnergy);
		const score = denom > 0 ? dot / denom : 0;
		if (score > best) {
			best = score;
			bestLag = lag;
		}
	}
	return { lagMs: (bestLag / sampleRate) * 1000, peakNormalized: best };
}

export function resolveCalibratedHeadDelayMs(lagMs: number, peakNormalized: number): number {
	if (!Number.isFinite(peakNormalized) || peakNormalized < CUE_LATENCY_PEAK_MIN) {
		throw new Error(
			`cue latency peak ${String(peakNormalized)} is too weak to trust (min ${CUE_LATENCY_PEAK_MIN})`
		);
	}
	if (!Number.isFinite(lagMs) || lagMs < -0.5 || lagMs > HEAD_DELAY_MAX_MS + 0.5) {
		throw new Error(`cue latency ${lagMs} ms is outside 0..${HEAD_DELAY_MAX_MS}`);
	}
	return Math.min(HEAD_DELAY_MAX_MS, Math.max(0, Math.round(lagMs)));
}

/** Drive play/record thunks then resolve a Mixxx HEAD DELAY integer. */
export async function measureCueLatencyMs(opts: {
	sampleRate: number;
	playAndRecord: (reference: Float32Array) => Promise<Float32Array>;
	clickMs?: number;
	periodMs?: number;
	clickCount?: number;
}): Promise<number> {
	const reference = cueLatencyClickTrain({
		sampleRate: opts.sampleRate,
		...(opts.clickMs !== undefined ? { clickMs: opts.clickMs } : {}),
		...(opts.periodMs !== undefined ? { periodMs: opts.periodMs } : {}),
		...(opts.clickCount !== undefined ? { clickCount: opts.clickCount } : {})
	});
	const captured = await opts.playAndRecord(reference);
	const { lagMs, peakNormalized } = crossCorrelateLagMs(reference, captured, opts.sampleRate);
	return resolveCalibratedHeadDelayMs(lagMs, peakNormalized);
}
