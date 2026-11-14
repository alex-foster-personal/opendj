/**
 * SPACE impulse v2 (FXVERB, #9819): the color knob's convolver impulse, generated
 * in-repo from a seed and never loaded from a file.
 *
 * Decorrelated stereo noise is split at `SPACE_CROSSOVER_HZ` by a Linkwitz-Riley
 * 4th-order pair (the two bands sum flat), and each band gets its own exponential
 * decay: the low band falls 60 dB over `seconds`, the high band in
 * `SPACE_DAMPING_RATIO` of that, so the tail darkens as it decays, like a room.
 * The ratio is mimicked from the JOS convention that RT60 at 4 kHz is about half
 * RT60 at 500 Hz (https://ccrma.stanford.edu/~jos/pasp/Achieving_Desired_Reverberation_Times.html).
 *
 * Level is explicit, not a Web Audio default. The high band is lifted so each band
 * carries the energy it would with no damping (Jot's tonal correction, same JOS
 * chapter), so the wet level of any input spectrum stays where it was. The whole
 * impulse is then scaled to `SPACE_IMPULSE_ENERGY_PER_SEC * seconds` of energy per
 * channel, which is the level the convolver's `normalize = true` gave the v1 noise
 * impulse at Web Audio's 44.1 kHz calibration rate, and the convolver runs with
 * `normalize = false`. Pure maths, no Web Audio, so node tests measure it directly.
 */

/** Seed of the shipped SPACE impulse (the v1 seed, kept). */
export const SPACE_IMPULSE_SEED = 0xc010f;

/** RT60 of the high band over RT60 of the low band (mimicked, JOS: 4 kHz about half of 500 Hz). */
export const SPACE_DAMPING_RATIO = 0.5;

/** Band split, the geometric mean of the 500 Hz and 4 kHz measuring bands. */
export const SPACE_CROSSOVER_HZ = 1414;

/** Per-channel energy per second of RT60: Web Audio `normalize` level at 44.1 kHz (RMS 0.00125, mimicked). */
export const SPACE_IMPULSE_ENERGY_PER_SEC = 44100 * 0.00125 ** 2;

type Channel = Float32Array<ArrayBuffer>;

//----------------------------------------------------------------------------- helpers

function _xorshift(seed: number): () => number {
	let state = seed >>> 0 || 1;
	return () => {
		state ^= state << 13;
		state ^= state >>> 17;
		state ^= state << 5;
		return ((state >>> 0) / 0xffffffff) * 2 - 1;
	};
}

/** RBJ cookbook 2nd-order Butterworth section, run twice for Linkwitz-Riley 4th order. */
function _lr4(
	x: Float64Array,
	type: 'lowpass' | 'highpass',
	hz: number,
	sampleRate: number
): Float64Array {
	const w = (2 * Math.PI * hz) / sampleRate;
	const alpha = Math.sin(w) / (2 * Math.SQRT1_2);
	const cos = Math.cos(w);
	const a0 = 1 + alpha;
	const b1 = (type === 'lowpass' ? 1 - cos : -(1 + cos)) / a0;
	const b0 = Math.abs(b1) / 2;
	const a1 = (-2 * cos) / a0;
	const a2 = (1 - alpha) / a0;
	let y = x;
	for (let pass = 0; pass < 2; pass++) {
		const input = y;
		y = new Float64Array(input.length);
		let [x1, x2, y1, y2] = [0, 0, 0, 0];
		for (let i = 0; i < input.length; i++) {
			y[i] = b0 * input[i] + b1 * x1 + b0 * x2 - a1 * y1 - a2 * y2;
			[x2, x1, y2, y1] = [x1, input[i], y1, y[i]];
		}
	}
	return y;
}

/** Envelope falling 60 dB over `length` samples, and its energy. */
function _envelope(length: number, decaySamples: number): { env: Float64Array; energy: number } {
	const env = new Float64Array(length);
	const perSample = Math.log(1000) / decaySamples;
	let energy = 0;
	for (let i = 0; i < length; i++) {
		env[i] = Math.exp(-perSample * i);
		energy += env[i] * env[i];
	}
	return { env, energy };
}

//----------------------------------------------------------------------------- impulse

/** Build the seeded stereo SPACE impulse with per-band decay, energy-normalised for `normalize = false`. */
export function spaceImpulse(
	sampleRate: number,
	seconds: number,
	seed = SPACE_IMPULSE_SEED
): [Channel, Channel] {
	if (!(Number.isFinite(sampleRate) && sampleRate > 0))
		throw new Error(`sampleRate ${sampleRate} is not a positive rate`);
	if (!(Number.isFinite(seconds) && seconds > 0))
		throw new Error(`seconds ${seconds} is not a positive RT60`);
	const length = Math.max(1, Math.round(sampleRate * seconds));
	const low = _envelope(length, length);
	const high = _envelope(length, length * SPACE_DAMPING_RATIO);
	const tonalCorrection = Math.sqrt(low.energy / high.energy);
	const next = _xorshift(seed);
	const noise = [new Float64Array(length), new Float64Array(length)];
	for (let i = 0; i < length; i++) {
		noise[0][i] = next();
		noise[1][i] = next();
	}
	const shaped = noise.map((x) => {
		const lowBand = _lr4(x, 'lowpass', SPACE_CROSSOVER_HZ, sampleRate);
		const highBand = _lr4(x, 'highpass', SPACE_CROSSOVER_HZ, sampleRate);
		const h = new Float64Array(length);
		for (let i = 0; i < length; i++)
			h[i] = lowBand[i] * low.env[i] + highBand[i] * high.env[i] * tonalCorrection;
		return h;
	});
	let energy = 0;
	for (const h of shaped) for (let i = 0; i < length; i++) energy += h[i] * h[i];
	const scale = Math.sqrt((SPACE_IMPULSE_ENERGY_PER_SEC * seconds) / (energy / shaped.length));
	const [left, right] = shaped.map((h) => Float32Array.from(h, (v) => v * scale));
	return [left, right];
}
