/**
 * Seeded deterministic PCM generators for web audio tests (issue #10402).
 *
 * Determinism rules:
 * 1. All math in float64; each sample is stored once into Float32Array (one rounding).
 * 2. Only + - * /, Math.sqrt, Math.abs, Math.floor, Math.round, Math.fround,
 *    Math.sin, Math.exp, Math.log. No Math.random, Date, performance.
 * 3. Every random number comes from the seeded xorshift32 below.
 * 4. signalBytes reads Float32Array buffers as little-endian V8 layout.
 *
 * [if] the same recipe renders different bytes on two calls [then] the module is not deterministic.
 * [if] a loop hit does not align with its grid beat [then] the grid is not the one that placed hits.
 */

export type SignalKind =
	| 'white'
	| 'pink'
	| 'click'
	| 'sweep'
	| 'impulse'
	| 'loop'
	| 'oneshot_kick'
	| 'oneshot_snare'
	| 'oneshot_hat';

export interface SignalRecipe {
	kind: SignalKind;
	seconds: number;
	sampleRate: number;
	seed: number;
	channels: 1 | 2;
	/** click, loop; default 120 */
	bpm?: number;
	/** click, loop; default: every whole beat that fits */
	bars?: number;
	/** click downbeat over other beats (default 6); loop kick accent (default 0) */
	accentDb?: number;
}

export interface BeatGrid {
	bpm: number;
	beats_ms: number[];
	downbeats_ms: number[];
	bar1_ms: number;
}

export interface Signal {
	left: Float32Array;
	right: Float32Array;
	/** When left and right share one buffer, how many channels signalBytes encodes. */
	channels?: 1 | 2;
	grid?: BeatGrid;
}

const FLT_MIN = 1.1754943508222875e-38;
const LEAD_IN_MS = 100;
const CLICK_MS = 10;
const IMPULSE_AT_MS = 10;
const KICK_MS = 150;
const SNARE_MS = 180;
const HAT_MS = 50;
const HAT_GAIN = 0.4;
const SWEEP_F1 = 20;
const SWEEP_F2_CAP = 20000;

export function dbToGain(db: number): number {
	return Math.exp((db * Math.LN10) / 20);
}

export const PEAK_LIMIT = dbToGain(-3);
const LEVEL = dbToGain(-6);

function store(out: Float32Array, i: number, v: number): void {
	const f = Math.fround(v);
	out[i] = f !== 0 && Math.abs(f) < FLT_MIN ? 0 : f;
}

function xorshift32(seed: number): () => number {
	let s = seed >>> 0 || 0x6d2b79f5;
	return () => {
		s ^= s << 13;
		s ^= s >>> 17;
		s ^= s << 5;
		s >>>= 0;
		return (s / 4294967296) * 2 - 1;
	};
}

function hitIndex(ms: number, sampleRate: number): number {
	return Math.round((ms * sampleRate) / 1000);
}

function beatMs(i: number, bpm: number): number {
	return LEAD_IN_MS + (i * 60000) / bpm;
}

function validateRecipe(recipe: SignalRecipe): number {
	const kinds: SignalKind[] = [
		'white',
		'pink',
		'click',
		'sweep',
		'impulse',
		'loop',
		'oneshot_kick',
		'oneshot_snare',
		'oneshot_hat'
	];
	if (!kinds.includes(recipe.kind)) {
		throw new RangeError('synth-signals: kind is not a known signal kind');
	}
	if (!Number.isFinite(recipe.seconds) || recipe.seconds <= 0 || recipe.seconds > 600) {
		throw new RangeError('synth-signals: seconds must be finite and in (0, 600]');
	}
	if (
		!Number.isInteger(recipe.sampleRate) ||
		recipe.sampleRate < 8000 ||
		recipe.sampleRate > 192000
	) {
		throw new RangeError('synth-signals: sampleRate must be an integer in [8000, 192000]');
	}
	if (recipe.channels !== 1 && recipe.channels !== 2) {
		throw new RangeError('synth-signals: channels must be 1 or 2');
	}
	if (!Number.isInteger(recipe.seed) || recipe.seed < 0 || recipe.seed > 2 ** 32 - 1) {
		throw new RangeError('synth-signals: seed must be an integer in [0, 2**32-1]');
	}
	if (recipe.bpm !== undefined) {
		if (!Number.isFinite(recipe.bpm) || recipe.bpm < 30 || recipe.bpm > 300) {
			throw new RangeError('synth-signals: bpm must be finite and in [30, 300]');
		}
	}
	if (recipe.bars !== undefined) {
		if (!Number.isInteger(recipe.bars) || recipe.bars <= 0) {
			throw new RangeError('synth-signals: bars must be a positive integer');
		}
	}
	if (recipe.accentDb !== undefined) {
		if (!Number.isFinite(recipe.accentDb) || recipe.accentDb < 0) {
			throw new RangeError('synth-signals: accentDb must be finite and >= 0');
		}
	}
	return Math.round(recipe.seconds * recipe.sampleRate);
}

function renderWhite(n: number, seed: number, out: Float32Array): void {
	const rng = xorshift32(seed);
	for (let i = 0; i < n; i++) {
		store(out, i, rng() * LEVEL);
	}
}

function renderPink(n: number, seed: number, out: Float32Array): void {
	const rng = xorshift32(seed);
	const buf = new Float64Array(n);
	let b0 = 0;
	let b1 = 0;
	let b2 = 0;
	let b3 = 0;
	let b4 = 0;
	let b5 = 0;
	let b6 = 0;
	for (let i = 0; i < n; i++) {
		const w = rng();
		b0 = 0.99886 * b0 + w * 0.0555179;
		b1 = 0.99332 * b1 + w * 0.0750759;
		b2 = 0.969 * b2 + w * 0.153852;
		b3 = 0.8665 * b3 + w * 0.3104856;
		b4 = 0.55 * b4 + w * 0.5329522;
		b5 = -0.7616 * b5 - w * 0.016898;
		const v = b0 + b1 + b2 + b3 + b4 + b5 + b6 + w * 0.5362;
		b6 = w * 0.115926;
		buf[i] = v;
	}
	let peak = 0;
	for (let i = 0; i < n; i++) {
		const a = Math.abs(buf[i]);
		if (a > peak) peak = a;
	}
	const scale = peak > 0 ? LEVEL / peak : 0;
	for (let i = 0; i < n; i++) {
		store(out, i, buf[i] * scale);
	}
}

function hannWindow(t: number, duration: number): number {
	if (t <= 0 || t >= duration) return 0;
	return 0.5 - 0.5 * Math.cos((2 * Math.PI * t) / duration);
}

function raisedCosineFade(t: number, fadeSec: number, totalSec: number): number {
	if (t < fadeSec) return 0.5 - 0.5 * Math.cos((Math.PI * t) / fadeSec);
	if (t > totalSec - fadeSec) {
		const u = totalSec - t;
		return 0.5 - 0.5 * Math.cos((Math.PI * u) / fadeSec);
	}
	return 1;
}

function buildBeatGrid(
	n: number,
	sampleRate: number,
	bpm: number,
	bars: number | undefined
): BeatGrid {
	const beats_ms: number[] = [];
	const downbeats_ms: number[] = [];
	const maxBeats = bars !== undefined ? bars * 4 : Number.MAX_SAFE_INTEGER;
	let i = 0;
	while (i < maxBeats) {
		const ms = beatMs(i, bpm);
		const idx = hitIndex(ms, sampleRate);
		if (idx >= n) break;
		beats_ms.push(ms);
		if (i % 4 === 0) downbeats_ms.push(ms);
		i += 1;
	}
	if (beats_ms.length === 0) {
		throw new RangeError('synth-signals: seconds too short for one beat at this bpm');
	}
	return {
		bpm,
		beats_ms,
		downbeats_ms,
		bar1_ms: beats_ms[0]
	};
}

function renderClickTemplate(sampleRate: number, peak: number): Float64Array {
	const frames = Math.max(1, Math.round((CLICK_MS * sampleRate) / 1000));
	const tpl = new Float64Array(frames);
	const freq = 2000;
	for (let i = 0; i < frames; i++) {
		const t = i / sampleRate;
		const win = hannWindow(t, CLICK_MS / 1000);
		tpl[i] = Math.sin(2 * Math.PI * freq * t) * win * peak;
	}
	return tpl;
}

function addTemplate(mix: Float64Array, tpl: Float64Array, at: number, gain: number): void {
	for (let j = 0; j < tpl.length; j++) {
		const idx = at + j;
		if (idx >= mix.length) break;
		mix[idx] += tpl[j] * gain;
	}
}

function renderKickTemplate(sampleRate: number): Float64Array {
	const frames = Math.max(1, Math.round((KICK_MS * sampleRate) / 1000));
	const tpl = new Float64Array(frames);
	let phase = 0;
	let peak = 0;
	for (let i = 0; i < frames; i++) {
		const t = i / sampleRate;
		const f = 50 + 100 * Math.exp(-t / 0.03);
		phase += (2 * Math.PI * f) / sampleRate;
		const env = Math.exp(-t / 0.045);
		let v = Math.sin(phase) * env;
		const endFade = 0.002;
		if (t > KICK_MS / 1000 - endFade) {
			const u = (KICK_MS / 1000 - t) / endFade;
			v *= 0.5 - 0.5 * Math.cos(Math.PI * u);
		}
		tpl[i] = v;
		const a = Math.abs(v);
		if (a > peak) peak = a;
	}
	const scale = peak > 0 ? LEVEL / peak : 0;
	for (let i = 0; i < frames; i++) tpl[i] *= scale;
	return tpl;
}

function renderSnareTemplate(sampleRate: number, seed: number): Float64Array {
	const frames = Math.max(1, Math.round((SNARE_MS * sampleRate) / 1000));
	const tpl = new Float64Array(frames);
	const rng = xorshift32(seed);
	let peak = 0;
	for (let i = 0; i < frames; i++) {
		const t = i / sampleRate;
		const v =
			0.5 * Math.sin(2 * Math.PI * 200 * t) * Math.exp(-t / 0.03) + rng() * Math.exp(-t / 0.05);
		tpl[i] = v;
		const a = Math.abs(v);
		if (a > peak) peak = a;
	}
	const scale = peak > 0 ? LEVEL / peak : 0;
	for (let i = 0; i < frames; i++) tpl[i] *= scale;
	return tpl;
}

const kickTplByRate = new Map<number, Float64Array>();
const hatTplByRateSeed = new Map<string, Float64Array>();

function cachedKickTemplate(sampleRate: number): Float64Array {
	let tpl = kickTplByRate.get(sampleRate);
	if (tpl === undefined) {
		tpl = renderKickTemplate(sampleRate);
		kickTplByRate.set(sampleRate, tpl);
	}
	return tpl;
}

function cachedHatTemplate(sampleRate: number, seed: number): Float64Array {
	const key = `${sampleRate}:${seed >>> 0}`;
	let tpl = hatTplByRateSeed.get(key);
	if (tpl === undefined) {
		tpl = renderHatTemplate(sampleRate, seed);
		hatTplByRateSeed.set(key, tpl);
	}
	return tpl;
}

function renderHatTemplate(sampleRate: number, seed: number): Float64Array {
	const frames = Math.max(1, Math.round((HAT_MS * sampleRate) / 1000));
	const tpl = new Float64Array(frames);
	const rng = xorshift32(seed);
	let peak = 0;
	let prev = 0;
	for (let i = 0; i < frames; i++) {
		const t = i / sampleRate;
		const w = rng();
		const hp = i === 0 ? w : w - prev;
		prev = w;
		const v = hp * Math.exp(-t / 0.012);
		tpl[i] = v;
		const a = Math.abs(v);
		if (a > peak) peak = a;
	}
	const scale = peak > 0 ? LEVEL / peak : 0;
	for (let i = 0; i < frames; i++) tpl[i] *= scale;
	return tpl;
}

function renderSweep(n: number, sampleRate: number, seconds: number, out: Float32Array): void {
	const f2 = Math.min(SWEEP_F2_CAP, 0.45 * sampleRate);
	const L = Math.log(f2 / SWEEP_F1);
	const T = seconds;
	const fadeSec = 0.01;
	for (let i = 0; i < n; i++) {
		const t = i / sampleRate;
		const phase = ((2 * Math.PI * SWEEP_F1 * T) / L) * (Math.exp((t * L) / T) - 1);
		const amp = LEVEL * raisedCosineFade(t, fadeSec, seconds);
		store(out, i, Math.sin(phase) * amp);
	}
}

function renderImpulse(n: number, sampleRate: number, out: Float32Array): void {
	for (let i = 0; i < n; i++) store(out, i, 0);
	const idx = hitIndex(IMPULSE_AT_MS, sampleRate);
	if (idx >= 0 && idx < n) store(out, idx, LEVEL);
}

function assertPeak(kind: SignalKind, left: Float32Array, right: Float32Array): void {
	let peak = 0;
	const check = (arr: Float32Array) => {
		for (let i = 0; i < arr.length; i++) {
			const a = Math.abs(arr[i]);
			if (a > peak) peak = a;
		}
	};
	check(left);
	if (right !== left) check(right);
	if (peak > PEAK_LIMIT) {
		const db = 20 * Math.log10(peak);
		throw new RangeError(`synth-signals: ${kind} peaks at ${db} dBFS, above -3 dBFS`);
	}
}

export function makeSignal(recipe: SignalRecipe): Signal {
	const n = validateRecipe(recipe);
	const bpm = recipe.bpm ?? 120;
	const accentDb = recipe.accentDb ?? (recipe.kind === 'click' ? 6 : 0);
	const bars = recipe.bars;
	const seed = recipe.seed;
	const sr = recipe.sampleRate;

	let left = new Float32Array(n);
	let right: Float32Array;
	let grid: BeatGrid | undefined;

	switch (recipe.kind) {
		case 'white': {
			renderWhite(n, seed, left);
			if (recipe.channels === 1) {
				right = left;
			} else {
				right = new Float32Array(n);
				renderWhite(n, (seed ^ 0x9e3779b9) >>> 0, right);
			}
			break;
		}
		case 'pink': {
			renderPink(n, seed, left);
			if (recipe.channels === 1) {
				right = left;
			} else {
				right = new Float32Array(n);
				renderPink(n, (seed ^ 0x9e3779b9) >>> 0, right);
			}
			break;
		}
		case 'click': {
			const mix = new Float64Array(n);
			grid = buildBeatGrid(n, sr, bpm, bars);
			const downGain = LEVEL;
			const upGain = LEVEL * dbToGain(-accentDb);
			const downTpl = renderClickTemplate(sr, downGain);
			const upTpl = renderClickTemplate(sr, upGain);
			const downSet = new Set(grid.downbeats_ms);
			for (const ms of grid.beats_ms) {
				const at = hitIndex(ms, sr);
				const tpl = downSet.has(ms) ? downTpl : upTpl;
				addTemplate(mix, tpl, at, 1);
			}
			for (let i = 0; i < n; i++) store(left, i, mix[i]);
			if (recipe.channels === 1) {
				right = left;
			} else {
				right = left.slice();
			}
			break;
		}
		case 'sweep': {
			renderSweep(n, sr, recipe.seconds, left);
			if (recipe.channels === 1) {
				right = left;
			} else {
				right = left.slice();
			}
			break;
		}
		case 'impulse': {
			renderImpulse(n, sr, left);
			if (recipe.channels === 1) {
				right = left;
			} else {
				right = left.slice();
			}
			break;
		}
		case 'loop': {
			const mix = new Float64Array(n);
			grid = buildBeatGrid(n, sr, bpm, bars);
			const kickTpl = cachedKickTemplate(sr);
			const hatTpl = cachedHatTemplate(sr, seed);
			const downGain = 1;
			const upGain = dbToGain(-accentDb);
			let beatIndex = 0;
			for (const ms of grid.beats_ms) {
				const at = hitIndex(ms, sr);
				const g = beatIndex % 4 === 0 ? downGain : upGain;
				addTemplate(mix, kickTpl, at, g);
				beatIndex += 1;
			}
			const eighthMs = 30000 / bpm;
			const maxBeats = bars !== undefined ? bars * 4 : grid.beats_ms.length;
			for (let i = 0; i < maxBeats; i++) {
				const offMs = beatMs(i, bpm) + eighthMs;
				const idx = hitIndex(offMs, sr);
				if (idx >= n) break;
				addTemplate(mix, hatTpl, idx, HAT_GAIN);
			}
			if (recipe.channels === 1) {
				for (let i = 0; i < n; i++) store(left, i, mix[i]);
				right = left;
			} else {
				right = new Float32Array(n);
				for (let i = 0; i < n; i++) {
					const v = mix[i];
					store(left, i, v);
					store(right, i, v);
				}
			}
			break;
		}
		case 'oneshot_kick': {
			const mix = new Float64Array(n);
			const tpl = renderKickTemplate(sr);
			addTemplate(mix, tpl, 0, 1);
			for (let i = 0; i < n; i++) store(left, i, mix[i]);
			if (recipe.channels === 1) {
				right = left;
			} else {
				right = left.slice();
			}
			break;
		}
		case 'oneshot_snare': {
			const mix = new Float64Array(n);
			const tpl = renderSnareTemplate(sr, seed);
			addTemplate(mix, tpl, 0, 1);
			for (let i = 0; i < n; i++) store(left, i, mix[i]);
			if (recipe.channels === 1) {
				right = left;
			} else {
				right = left.slice();
			}
			break;
		}
		case 'oneshot_hat': {
			const mix = new Float64Array(n);
			const tpl = renderHatTemplate(sr, seed);
			addTemplate(mix, tpl, 0, 1);
			for (let i = 0; i < n; i++) store(left, i, mix[i]);
			if (recipe.channels === 1) {
				right = left;
			} else {
				right = left.slice();
			}
			break;
		}
		default: {
			const _exhaustive: never = recipe.kind;
			throw new RangeError(`synth-signals: unhandled kind ${_exhaustive}`);
		}
	}

	assertPeak(recipe.kind, left, right);

	const signal: Signal = { left, right, channels: recipe.channels };
	if (grid !== undefined) {
		signal.grid = grid;
	}
	return signal;
}

export function signalBytes(signal: Signal): Uint8Array {
	if (signal.left === signal.right) {
		const leftBytes = new Uint8Array(
			signal.left.buffer,
			signal.left.byteOffset,
			signal.left.byteLength
		);
		if (signal.channels === 2) {
			const out = new Uint8Array(leftBytes.byteLength * 2);
			out.set(leftBytes, 0);
			out.set(leftBytes, leftBytes.byteLength);
			return out;
		}
		return leftBytes.slice();
	}
	const out = new Uint8Array(signal.left.byteLength + signal.right.byteLength);
	out.set(new Uint8Array(signal.left.buffer, signal.left.byteOffset, signal.left.byteLength), 0);
	out.set(
		new Uint8Array(signal.right.buffer, signal.right.byteOffset, signal.right.byteLength),
		signal.left.byteLength
	);
	return out;
}

// impulse is omitted on purpose: one non-zero sample is pinned structurally in tests.
const defaultRecipeTable = {
	white_2s: {
		kind: 'white' as const,
		seconds: 2,
		sampleRate: 48000,
		seed: 10402,
		channels: 2 as const
	},
	pink_2s: {
		kind: 'pink' as const,
		seconds: 2,
		sampleRate: 48000,
		seed: 10402,
		channels: 2 as const
	},
	click_120_4bars: {
		kind: 'click' as const,
		seconds: 8.2,
		sampleRate: 48000,
		seed: 10402,
		channels: 2 as const,
		bpm: 120,
		bars: 4,
		accentDb: 6
	},
	sweep_2s: {
		kind: 'sweep' as const,
		seconds: 2,
		sampleRate: 48000,
		seed: 10402,
		channels: 2 as const
	},
	loop_124_4bars: {
		kind: 'loop' as const,
		seconds: 8,
		sampleRate: 48000,
		seed: 10402,
		channels: 2 as const,
		bpm: 124,
		bars: 4
	},
	oneshot_kick: {
		kind: 'oneshot_kick' as const,
		seconds: 0.25,
		sampleRate: 48000,
		seed: 10402,
		channels: 2 as const
	},
	oneshot_snare: {
		kind: 'oneshot_snare' as const,
		seconds: 0.25,
		sampleRate: 48000,
		seed: 10402,
		channels: 2 as const
	},
	oneshot_hat: {
		kind: 'oneshot_hat' as const,
		seconds: 0.1,
		sampleRate: 48000,
		seed: 10402,
		channels: 2 as const
	}
};

export const DEFAULT_RECIPES: Readonly<Record<string, SignalRecipe>> = Object.freeze(
	Object.fromEntries(
		Object.entries(defaultRecipeTable).map(([k, v]) => [k, Object.freeze({ ...v })])
	)
);
