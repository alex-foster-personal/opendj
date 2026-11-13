/**
 * Cut move metrics on per-deck renders (FXTRANS): Tail Intrusion Ratio and landing error.
 *
 * Pure: no AudioContext, no deck state. Inputs are offline per-deck renders, each its own
 * channels at one sample rate. TIR is the K-weighted energy of everything derived from the
 * outgoing deck (dry plus FX return) over the incoming deck's energy, in dB, over two
 * windows: the first beat after T_in and the late tail [T_in + 1 bar, T_in + 2 bars].
 * Landing error is the incoming deck's first low band (40..150 Hz) onset minus T_in; a
 * render whose |error| is over 10 ms is excluded from rating (hard gate).
 *
 * Each window is K-weighted from rest, so energy from before the window never leaks into it
 * through the filters' memory: a dry outgoing that stops at T_in reads -Infinity dB.
 *
 * Mimicked (hygiene, no calibration): K-weighting is ITU-R BS.1770-4 Annex 1 (pre-filter
 * shelf plus RLB high-pass, coefficients re-derived per sample rate as libebur128 does).
 * Spec `(private path)` section 6; TIR defined in
 * `(private path)` section 7.
 */

/** One deck rendered alone: its channels at one sample rate. */
export interface DeckRender {
	sampleRate: number;
	channels: readonly ArrayLike<number>[];
}

/** TIR in dB over the first beat and the late tail window. */
export interface TailIntrusion {
	firstBeatDb: number;
	lateTailDb: number;
}

/** One rendered clip's landing error, for the rating gate. */
export interface LandingClip {
	id: string;
	landingErrorMs: number;
}

/** Biquad coefficients, a0 normalised to 1. */
export interface Biquad {
	b0: number;
	b1: number;
	b2: number;
	a1: number;
	a2: number;
}

/** Beats per bar on the 4/4 grid the windows count in. */
export const BEATS_PER_BAR = 4;

/** Low band the landing onset is read in, Hz (kick fundamental). */
export const LANDING_BAND_HZ = Object.freeze({ low: 40, high: 150 });

/** Search span around T_in for the landing onset, seconds. */
export const LANDING_SEARCH_SEC = Object.freeze({ before: 0.1, after: 0.2 });

/** Onset is the first sample whose band magnitude reaches this fraction of the span's peak. */
export const LANDING_ONSET_FRACTION = 0.5;

/** A clip whose |landing error| exceeds this is dropped from rating (spec hard gate). */
export const LANDING_GATE_MS = 10;

//-----------------------------------------------------------------------------
// filters

/** BS.1770-4 K-weighting stages (pre-filter shelf, RLB high-pass) at a sample rate. */
export function kWeightingBiquads(sampleRate: number): [Biquad, Biquad] {
	_assertSampleRate(sampleRate);
	const shelfK = Math.tan((Math.PI * 1681.974450955533) / sampleRate);
	const shelfQ = 0.7071752369554196;
	const vh = 10 ** (3.999843853973347 / 20);
	const vb = vh ** 0.4996667741545416;
	const shelfA0 = 1 + shelfK / shelfQ + shelfK * shelfK;
	const shelf: Biquad = {
		b0: (vh + (vb * shelfK) / shelfQ + shelfK * shelfK) / shelfA0,
		b1: (2 * (shelfK * shelfK - vh)) / shelfA0,
		b2: (vh - (vb * shelfK) / shelfQ + shelfK * shelfK) / shelfA0,
		a1: (2 * (shelfK * shelfK - 1)) / shelfA0,
		a2: (1 - shelfK / shelfQ + shelfK * shelfK) / shelfA0
	};
	const hpK = Math.tan((Math.PI * 38.13547087602444) / sampleRate);
	const hpQ = 0.5003270373238773;
	const hpA0 = 1 + hpK / hpQ + hpK * hpK;
	const highPass: Biquad = {
		b0: 1,
		b1: -2,
		b2: 1,
		a1: (2 * (hpK * hpK - 1)) / hpA0,
		a2: (1 - hpK / hpQ + hpK * hpK) / hpA0
	};
	return [shelf, highPass];
}

function _butterworth(kind: 'lowpass' | 'highpass', cutoffHz: number, sampleRate: number): Biquad {
	const w = (2 * Math.PI * cutoffHz) / sampleRate;
	const alpha = Math.sin(w) / (2 * Math.SQRT1_2);
	const cos = Math.cos(w);
	const a0 = 1 + alpha;
	const edge = kind === 'lowpass' ? (1 - cos) / 2 : (1 + cos) / 2;
	const mid = kind === 'lowpass' ? 1 - cos : -(1 + cos);
	return { b0: edge / a0, b1: mid / a0, b2: edge / a0, a1: (-2 * cos) / a0, a2: (1 - alpha) / a0 };
}

function _runBiquad(input: ArrayLike<number>, f: Biquad, from: number, to: number): Float64Array {
	const out = new Float64Array(to - from);
	let x1 = 0;
	let x2 = 0;
	let y1 = 0;
	let y2 = 0;
	for (let i = from; i < to; i++) {
		const x = input[i];
		const y = f.b0 * x + f.b1 * x1 + f.b2 * x2 - f.a1 * y1 - f.a2 * y2;
		x2 = x1;
		x1 = x;
		y2 = y1;
		y1 = y;
		out[i - from] = y;
	}
	return out;
}

function _reversed(values: Float64Array): Float64Array {
	return values.slice().reverse();
}

//-----------------------------------------------------------------------------
// validation

function _assertSampleRate(sampleRate: number): void {
	if (!Number.isFinite(sampleRate) || sampleRate <= 0) {
		throw new RangeError(`sampleRate must be a positive finite number, got ${sampleRate}`);
	}
}

function _assertRender(render: DeckRender, name: string): number {
	_assertSampleRate(render.sampleRate);
	if (render.channels.length === 0) throw new RangeError(`${name} render has no channels`);
	const length = render.channels[0].length;
	if (render.channels.some((ch) => ch.length !== length)) {
		throw new RangeError(`${name} render channels differ in length`);
	}
	return length;
}

function _windowSamples(
	startSec: number,
	lengthSec: number,
	sampleRate: number,
	renderLength: number,
	name: string
): [number, number] {
	const from = Math.round(startSec * sampleRate);
	const to = Math.round((startSec + lengthSec) * sampleRate);
	if (from < 0 || to > renderLength || to <= from) {
		throw new RangeError(
			`${name} window [${startSec}, ${startSec + lengthSec}] s is outside the render`
		);
	}
	return [from, to];
}

//-----------------------------------------------------------------------------
// metrics

function _kWeightedEnergy(render: DeckRender, from: number, to: number): number {
	const [shelf, highPass] = kWeightingBiquads(render.sampleRate);
	let energy = 0;
	for (const ch of render.channels) {
		const weighted = _runBiquad(_runBiquad(ch, shelf, from, to), highPass, 0, to - from);
		for (const y of weighted) energy += y * y;
	}
	return energy;
}

function _intrusionDb(
	outgoing: DeckRender,
	incoming: DeckRender,
	from: number,
	to: number,
	name: string
): number {
	const incomingEnergy = _kWeightedEnergy(incoming, from, to);
	if (!(incomingEnergy > 0)) throw new RangeError(`incoming deck is silent in the ${name} window`);
	return 10 * Math.log10(_kWeightedEnergy(outgoing, from, to) / incomingEnergy);
}

/** TIR: K-weighted outgoing over incoming energy in [T_in, T_in + 1 beat] and [T_in + 1 bar, T_in + 2 bars], dB. */
export function tailIntrusionDb(
	outgoing: DeckRender,
	incoming: DeckRender,
	tInSec: number,
	beatSec: number
): TailIntrusion {
	const outLength = _assertRender(outgoing, 'outgoing');
	const inLength = _assertRender(incoming, 'incoming');
	if (outgoing.sampleRate !== incoming.sampleRate) {
		throw new RangeError(
			`deck renders differ in sample rate: ${outgoing.sampleRate} vs ${incoming.sampleRate}`
		);
	}
	if (!Number.isFinite(beatSec) || beatSec <= 0)
		throw new RangeError(`beatSec must be positive, got ${beatSec}`);
	const length = Math.min(outLength, inLength);
	const barSec = BEATS_PER_BAR * beatSec;
	const [beatFrom, beatTo] = _windowSamples(
		tInSec,
		beatSec,
		outgoing.sampleRate,
		length,
		'first beat'
	);
	const [lateFrom, lateTo] = _windowSamples(
		tInSec + barSec,
		barSec,
		outgoing.sampleRate,
		length,
		'late tail'
	);
	return {
		firstBeatDb: _intrusionDb(outgoing, incoming, beatFrom, beatTo, 'first beat'),
		lateTailDb: _intrusionDb(outgoing, incoming, lateFrom, lateTo, 'late tail')
	};
}

/** Landing error: the incoming deck's first 40..150 Hz onset near T_in minus T_in, in ms. */
export function landingErrorMs(incoming: DeckRender, tInSec: number): number {
	const length = _assertRender(incoming, 'incoming');
	const sr = incoming.sampleRate;
	const [from, to] = _windowSamples(
		tInSec - LANDING_SEARCH_SEC.before,
		LANDING_SEARCH_SEC.before + LANDING_SEARCH_SEC.after,
		sr,
		length,
		'landing search'
	);
	const highPass = _butterworth('highpass', LANDING_BAND_HZ.low, sr);
	const lowPass = _butterworth('lowpass', LANDING_BAND_HZ.high, sr);
	const magnitude = new Float64Array(length);
	for (const ch of incoming.channels) {
		// Zero phase (forward then backward), so the band filters add no onset delay.
		const forward = _runBiquad(_runBiquad(ch, highPass, 0, length), lowPass, 0, length);
		const band = _reversed(
			_runBiquad(_runBiquad(_reversed(forward), highPass, 0, length), lowPass, 0, length)
		);
		for (let i = 0; i < length; i++) magnitude[i] = Math.max(magnitude[i], Math.abs(band[i]));
	}
	let peak = 0;
	for (let i = from; i < to; i++) peak = Math.max(peak, magnitude[i]);
	if (!(peak > 0)) throw new RangeError('incoming deck has no 40..150 Hz energy near T_in');
	let onset = from;
	while (magnitude[onset] < LANDING_ONSET_FRACTION * peak) onset++;
	return (onset / sr - tInSec) * 1000;
}

/** Rating gate: keep clips with |landing error| <= 10 ms, list the rest by id. */
export function splitByLandingGate<T extends LandingClip>(
	clips: readonly T[]
): { kept: T[]; droppedIds: string[] } {
	const kept: T[] = [];
	const droppedIds: string[] = [];
	for (const clip of clips) {
		if (!Number.isFinite(clip.landingErrorMs))
			throw new RangeError(`clip ${clip.id} has a non-finite landing error`);
		if (Math.abs(clip.landingErrorMs) <= LANDING_GATE_MS) kept.push(clip);
		else droppedIds.push(clip.id);
	}
	return { kept, droppedIds };
}
