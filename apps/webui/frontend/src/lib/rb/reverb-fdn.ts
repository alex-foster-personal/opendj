/**
 * FDN reverb kernel maths (FXVERB, issue #9817; spec fx-fanout-20261108/05-reverb.md sections 4, 6).
 *
 * Eight delay lines with Hadamard input and output vectors, a Householder
 * feedback matrix, a per-line one-pole damping filter and a per-line decay
 * gain from RT60, behind a pre-delay line and a freeze. Written from the JOS
 * public pages (Householder_Feedback_Matrix, Choice_Delay_Lengths,
 * Achieving_Desired_Reverberation_Times) in our own code; nothing is copied.
 *
 * The kernel runs in `reverb-fdn-processor.js` (an AudioWorklet cannot import),
 * so this module holds the constants, the delay-length table the processor
 * takes in `processorOptions`, and the coefficient laws the processor mirrors.
 * Change one, change the other: the unit test compares them.
 *
 * Damping: `rt60` is the decay time at 500 Hz and `damping` is RT60(4 kHz) /
 * RT60(500 Hz). Each line's pole is solved exactly at both frequencies. One
 * pole cannot reach every ratio on every line; `damping` stops at 0.5 so the
 * longest line at the shortest RT60 stays reachable.
 */

//--------------------------------------------------------------------- constants

export const REVERB_FDN_PROCESSOR_NAME = 'mdt-reverb-fdn';
export const REVERB_FDN_LINES = 8;
/** Delay lengths are designed at 48 kHz and size 1, then scaled to the context rate. */
export const REVERB_FDN_REFERENCE_RATE = 48000;
/** Shortest and longest line at the reference (21 and 50 ms), geometric between, then prime. */
export const REVERB_FDN_REFERENCE_MIN_SAMPLES = 1000;
export const REVERB_FDN_REFERENCE_MAX_SAMPLES = 2400;
/** `size` snaps to this grid; each step has its own prime length set. */
export const REVERB_FDN_SIZE_STEP = 0.05;
/** The two frequencies `rt60` and `damping` are defined at. */
export const REVERB_FDN_DAMPING_LOW_HZ = 500;
export const REVERB_FDN_DAMPING_HIGH_HZ = 4000;
/** Freeze and input gate ramp (MIMICKED from the spec's 10 ms Release FX cut). */
export const REVERB_FDN_RAMP_SEC = 0.01;
/** Line and filter state below this is flushed to 0 (no subnormals). */
export const REVERB_FDN_DENORMAL_FLUSH = 1e-25;
/**
 * Return level trim after the unit-energy normalisation. Keeps -1 dBFS uniform
 * noise (RMS about -6 dBFS, louder than any track) under 0 dBFS at rt60 10 s,
 * damping off. The full-wet ceiling itself is calibration-pending (#9824).
 */
export const REVERB_FDN_OUTPUT_TRIM = 0.3;

/**
 * AudioParam ranges. rt60 0.3..10 s and pre-delay 0..100 ms (default 20 ms) per
 * the issue; rt60 default 2.6 s is today's SPACE tail; damping default 0.5 is
 * MIMICKED from the JOS high-frequencies-decay-faster convention; freeze is
 * MIMICKED from Traktor's Reverb FREEZE (input closed, output open).
 */
export const REVERB_FDN_PARAMS = {
	rt60: { min: 0.3, max: 10, default: 2.6 },
	size: { min: 0.5, max: 1.5, default: 1 },
	predelay: { min: 0, max: 0.1, default: 0.02 },
	damping: { min: 0.5, max: 1, default: 0.5 },
	wet: { min: 0, max: 1, default: 1 },
	freeze: { min: 0, max: 1, default: 0 }
} as const;

export type ReverbFdnParamName = keyof typeof REVERB_FDN_PARAMS;
export type ReverbFdnParams = Record<ReverbFdnParamName, number>;

//--------------------------------------------------------------------- helpers

function _isPrime(n: number): boolean {
	if (n < 2) return false;
	for (let d = 2; d * d <= n; d++) if (n % d === 0) return false;
	return true;
}

function _nextPrime(n: number): number {
	let p = Math.max(2, Math.ceil(n));
	while (!_isPrime(p)) p++;
	return p;
}

function _requireSampleRate(sampleRate: number): void {
	if (!Number.isFinite(sampleRate) || sampleRate < 8000 || sampleRate > 192000) {
		throw new RangeError(`reverb sampleRate must be 8000..192000; got ${sampleRate}`);
	}
}

//--------------------------------------------------------------------- laws

/** Every size grid point from the size min to max, in REVERB_FDN_SIZE_STEP steps. */
export function reverbFdnSizeGrid(): number[] {
	const { min, max } = REVERB_FDN_PARAMS.size;
	const steps = Math.round((max - min) / REVERB_FDN_SIZE_STEP);
	return Array.from({ length: steps + 1 }, (_, i) => min + i * REVERB_FDN_SIZE_STEP);
}

/** Eight distinct prime (so mutually prime) line lengths in samples for a size at a rate. */
export function reverbFdnDelayLengths(sampleRate: number, size: number): number[] {
	_requireSampleRate(sampleRate);
	const { min, max } = REVERB_FDN_PARAMS.size;
	if (!Number.isFinite(size) || size < min || size > max) {
		throw new RangeError(`reverb size must be ${min}..${max}; got ${size}`);
	}
	const ratio = REVERB_FDN_REFERENCE_MAX_SAMPLES / REVERB_FDN_REFERENCE_MIN_SAMPLES;
	const scale = (size * sampleRate) / REVERB_FDN_REFERENCE_RATE;
	const lengths: number[] = [];
	for (let i = 0; i < REVERB_FDN_LINES; i++) {
		const target =
			REVERB_FDN_REFERENCE_MIN_SAMPLES * Math.pow(ratio, i / (REVERB_FDN_LINES - 1)) * scale;
		lengths.push(_nextPrime(Math.max(Math.round(target), i > 0 ? lengths[i - 1] + 1 : 2)));
	}
	return lengths;
}

/** Per-line loop gain so each line loses 60 dB in rt60 seconds: 10^(-3 L / (rt60 fs)). */
export function reverbFdnDecayGains(lengths: number[], rt60: number, sampleRate: number): number[] {
	return lengths.map((length) => Math.pow(10, (-3 * length) / (rt60 * sampleRate)));
}

/**
 * Per-line one-pole y = b x + a y[-1] whose gain is the RT60 decay at 500 Hz and
 * the RT60 x damping decay at 4 kHz, plus the unit-energy output gain.
 */
export function reverbFdnLoopCoefficients(
	lengths: number[],
	rt60: number,
	damping: number,
	sampleRate: number
): { b: number[]; a: number[]; outputGain: number } {
	const c1 = Math.cos((2 * Math.PI * REVERB_FDN_DAMPING_LOW_HZ) / sampleRate);
	const c2 = Math.cos((2 * Math.PI * REVERB_FDN_DAMPING_HIGH_HZ) / sampleRate);
	const lows = reverbFdnDecayGains(lengths, rt60, sampleRate);
	const highs = reverbFdnDecayGains(lengths, rt60 * damping, sampleRate);
	const b: number[] = [];
	const a: number[] = [];
	for (let i = 0; i < lengths.length; i++) {
		const r = (lows[i] * lows[i]) / (highs[i] * highs[i]);
		let pole = 0;
		if (r - 1 > 1e-12) {
			const mid = (r * c1 - c2) / (r - 1);
			if (!(mid > 1)) {
				throw new RangeError(`reverb damping ${damping} unreachable at rt60 ${rt60}`);
			}
			pole = mid - Math.sqrt(mid * mid - 1);
		}
		a.push(pole);
		b.push(lows[i] * Math.sqrt(1 - 2 * pole * c1 + pole * pole));
	}
	const mean = lengths.reduce((s, l) => s + l, 0) / lengths.length;
	const g = Math.pow(10, (-3 * mean) / (rt60 * sampleRate));
	return { b, a, outputGain: Math.sqrt(REVERB_FDN_LINES * (1 - g * g)) * REVERB_FDN_OUTPUT_TRIM };
}

/** Throws a RangeError naming the first parameter that is non-finite or out of range. */
export function validateReverbFdnParams(params: ReverbFdnParams): void {
	for (const name of Object.keys(REVERB_FDN_PARAMS) as ReverbFdnParamName[]) {
		const { min, max } = REVERB_FDN_PARAMS[name];
		const value = params[name];
		if (!Number.isFinite(value) || value < min || value > max) {
			throw new RangeError(`reverb ${name} must be ${min}..${max}; got ${value}`);
		}
	}
	if (params.freeze !== 0 && params.freeze !== 1) {
		throw new RangeError(`reverb freeze must be 0 or 1; got ${params.freeze}`);
	}
}

/** The processorOptions the worklet needs at a context rate: every number it uses. */
export function reverbFdnProcessorOptions(sampleRate: number) {
	_requireSampleRate(sampleRate);
	return {
		sizes: reverbFdnSizeGrid(),
		lengthTable: reverbFdnSizeGrid().flatMap((size) => reverbFdnDelayLengths(sampleRate, size)),
		maxPredelaySamples: Math.ceil(REVERB_FDN_PARAMS.predelay.max * sampleRate),
		lowHz: REVERB_FDN_DAMPING_LOW_HZ,
		highHz: REVERB_FDN_DAMPING_HIGH_HZ,
		rampSec: REVERB_FDN_RAMP_SEC,
		denormalFlush: REVERB_FDN_DENORMAL_FLUSH,
		outputTrim: REVERB_FDN_OUTPUT_TRIM
	};
}
