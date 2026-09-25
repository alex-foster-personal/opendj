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

/**
 * Stage one: the chirp gains tried in order until the mic hears the bus.
 *
 * A normalized cross-correlation is amplitude invariant, so gain does not make
 * a heard signal score higher; it only lifts a signal off the room's noise
 * floor. Measured on this Mac Wed 16 Sep 2026 with a USB mic 30 cm away, the
 * cue leg scored 0.09-0.31 at 0.12 and cleared 0.35 from about 0.25 upward,
 * with nothing above 0.5 improving it. So the ladder starts below the measured
 * floor, stops at the FIRST rung that clears the threshold, and never pushes a
 * cup against a microphone harder than it has to.
 *
 * This is also the only volume lever the app owns for the cue bus: a page
 * cannot read or set an output device's hardware volume, so if the ladder runs
 * out the remaining fix is the operator's to make in the OS.
 */
export const CUE_LATENCY_GAIN_STEPS: readonly number[] = [0.15, 0.3, 0.5, 0.75, 1];

/**
 * Stage one keeps climbing until it hears a peak this good, not merely one
 * that clears `CUE_LATENCY_PEAK_MIN`.
 *
 * Measured live Wed 16 Sep 2026 (real Chromium, MX Brio, cue on the headphone
 * jack): the level find settled on a rung scoring 0.498, and a confirming
 * chirp at that same rung then scored 0.29 and failed the run. A rung that
 * only just clears the refusal line has no headroom for a cup shifting or a
 * noise in the room, so the ramp pays a few more chirps to find one that does.
 * Nothing below the refusal line is ever accepted; this only decides when to
 * STOP climbing.
 */
export const CUE_LATENCY_PEAK_TARGET = 0.5;

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

/**
 * The calibration probe: ONE exponential sine sweep, the standard excitation for
 * measuring an acoustic path (Farina). It replaces the click train for alignment
 * because four identical clicks 60 ms apart correlate with themselves at +/-60 ms
 * almost as well as at zero, so a room echo moved the measured lag by exactly one
 * period (Wed 16 Sep 2026: speakers 153, 85, 153 ms at a steady 0.52 peak). A sweep
 * never repeats, so its correlation has one peak.
 *
 * 300 Hz to 6 kHz: laptop speakers roll off below ~300 Hz and every mic and headphone
 * in scope passes 6 kHz. 200 ms fits the capture window the click train was sized for.
 */
export const CUE_LATENCY_SWEEP_MS = 200;
export const CUE_LATENCY_SWEEP_START_HZ = 300;
export const CUE_LATENCY_SWEEP_END_HZ = 6000;
export const CUE_LATENCY_SWEEP_FADE_MS = 10;

export function cueLatencySweep(opts: { sampleRate: number }): Float32Array {
	const sampleRate = opts.sampleRate;
	if (!Number.isFinite(sampleRate) || sampleRate <= 0) {
		throw new RangeError(`cue latency sampleRate must be positive, got ${sampleRate}`);
	}
	const total = Math.round((CUE_LATENCY_SWEEP_MS / 1000) * sampleRate);
	const durationSec = total / sampleRate;
	const ratio = Math.log(CUE_LATENCY_SWEEP_END_HZ / CUE_LATENCY_SWEEP_START_HZ);
	const fade = Math.max(1, Math.round((CUE_LATENCY_SWEEP_FADE_MS / 1000) * sampleRate));
	const out = new Float32Array(total);
	for (let i = 0; i < total; i += 1) {
		const t = i / sampleRate;
		// Phase of an exponential sweep: 2*pi*f0*T/ln(f1/f0) * (e^(t*ln(f1/f0)/T) - 1).
		const phase = ((2 * Math.PI * CUE_LATENCY_SWEEP_START_HZ * durationSec) / ratio) * (Math.exp((t * ratio) / durationSec) - 1);
		const edge = Math.min(i, total - 1 - i);
		const taper = edge >= fade ? 1 : 0.5 * (1 - Math.cos((Math.PI * edge) / fade));
		out[i] = 0.9 * Math.sin(phase) * taper;
	}
	return out;
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
