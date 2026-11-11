#!/usr/bin/env node
/**
 * Export blind A/B pairs for flanger and phaser voicing calibration (#9838, FXMOD-9838).
 *
 * Spec: (private path) section 6.
 *
 * Calibration clips render ONLY through a real-path renderer: the #9774 fx render harness
 * driving mod-fx-processor.js (#9834 flanger, #9835 phaser). Until those land and an adapter
 * is wired into resolveRealRenderer, the CLI prints [UNAVAILABLE], exits 2 and writes nothing.
 *
 * Usage:
 *   node scripts/export-mod-fx-rating-pairs.mjs --out <dir> [--seed <uint32>] [--bars <n>]
 *       [--sample-rate <hz>] [--fixture <name>=<path.wav>]...
 *
 * Mini-PRD:
 *   R0 no real render path means UNAVAILABLE, never a calibration from a reimplementation
 *     [if] any real-path dependency file is missing [then ⛔️ unless the CLI exits 2 with [UNAVAILABLE] naming each missing issue and path]
 *     [if] UNAVAILABLE [then ⛔️ if --out was created or written]
 *     [if] exportModFxRatingPairs gets a renderer with realPath !== true and no allowTestDouble [then ⛔️ unless it throws]
 *   R1 writes WAVs plus pairs.json (#9776 shape) to --out: 24 pairs, sides and order from a recorded seed
 *     [if] same seed twice [then ⛔️ if manifests differ]
 *     [if] --out is missing or non-empty [then ⛔️ unless the CLI exits 1 naming it]
 *   R2 every render passes hard vetoes before any file is written
 *     [if] a render exceeds 0 dBFS [then ⛔️ unless VetoError peak and an empty out dir]
 *     [if] a render holds NaN [then ⛔️ unless VetoError finite]
 *     [if] mono fold loss exceeds 3 dB [then ⛔️ unless VetoError mono-fold]
 *   R3 manifest in the #9776 page shape with proxies under meta (hidden from the page)
 *     [if] hidden_fields omits meta [then ⛔️]
 *   (R1-R3 are exercised under a test double only; they carry no real-path evidence yet.)
 */
import { mkdirSync, readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

/**
 * @typedef {{ feedback?: number, spreadDeg: number, cycleBeats: number, depthCurve: 'linear'|'equal-power', stages?: 4|8 }} Voicing
 * @typedef {{ kernel: 'flanger'|'phaser', voicing: Voicing, depth: number, input: Float64Array[], sampleRate: number,
 *   bpm: number, firstDownbeatSec: number }} RenderRequest
 * @typedef {{ id: string, realPath: boolean, render(req: RenderRequest): Promise<Float64Array[]> }} Renderer
 */

//----------------------------------------------------------------------------- CFG
export const DEFAULT_SEED = 0x5eed9838;
export const SAMPLE_RATE = 48000;
export const BARS = 8;
export const BEATS_PER_BAR = 4;
export const PAIR_DEPTH = 0.5;
export const PROXY_DEPTHS = [0.5, 1];
export const PINK_BPM = 120;
export const DRY_RMS_DBFS = -23;
export const FIXTURE_PEAK_MAX_DBFS = -1;
export const MONO_FOLD_MAX_DB = 3;
export const QUESTION = 'Which sounds better as a DJ flanger or phaser?';
export const EXIT_UNAVAILABLE = 2;
export const KERNELS = ['flanger', 'phaser'];
export const REAL_PATH_DEPENDENCIES = Object.freeze([
	{ issue: '#9774', what: 'fx render harness (OfflineAudioContext host)', path: 'apps/webui/frontend/tests/e2e/support/fx-render-harness.ts' },
	{ issue: '#9834', what: 'mod FX processor with the FLANGER kernel', path: 'apps/webui/frontend/src/lib/rb/mod-fx-processor.js' },
	// #9835 extends mod-fx-processor.js; presence is marked by its unit test file
	{ issue: '#9835', what: 'PHASER kernel in the mod FX processor', path: 'apps/webui/frontend/tests/unit/mod-fx-phaser.test.mjs' },
	{ issue: '#9776', what: 'fx A/B rating page manifest schema', path: 'tools/fx-ab/manifest.schema.json' }
]);
const REPO_ROOT = fileURLToPath(new URL('..', import.meta.url));
const FLT_MIN_NORMAL = 1.1754943508222875e-38;
const DENORMAL_FLUSH = 1e-30;

export const BASELINE = Object.freeze({
	flanger: { feedback: 0.55, spreadDeg: 0, cycleBeats: 16, depthCurve: 'linear' },
	phaser: { stages: 4, feedback: 0.4, spreadDeg: 0, cycleBeats: 8, depthCurve: 'linear' }
});
export const AXES = Object.freeze([
	{ kernel: 'flanger', axis: 'feedback', values: [0.3, 0.55, 0.75] },
	{ kernel: 'flanger', axis: 'spreadDeg', values: [0, 45, 90] },
	{ kernel: 'flanger', axis: 'cycleBeats', values: [8, 16, 32] },
	{ kernel: 'flanger', axis: 'depthCurve', values: ['linear', 'equal-power'] },
	{ kernel: 'phaser', axis: 'stages', values: [4, 8] },
	{ kernel: 'phaser', axis: 'feedback', values: [0, 0.4, 0.65] },
	{ kernel: 'phaser', axis: 'cycleBeats', values: [4, 8, 16] },
	{ kernel: 'phaser', axis: 'depthCurve', values: ['linear', 'equal-power'] }
]);
export const FIXTURES = Object.freeze([
	{ name: 'house-loop', bpm: 124 },
	{ name: 'vocal-phrase', bpm: 100 },
	{ name: 'hihat-break', bpm: 174 }
]);

const VETOES_NOT_APPLIED = [
	{ veto: 'max-feedback-peak', reason: 'depth 1 at the code-capped feedback (0.9 flanger, 0.75 phaser) is a #9834/#9835 harness case' },
	{ veto: 'added-latency', reason: 'measured by the #9774 harness in #9834/#9835' },
	{ veto: 'dry-bit-exact', reason: 'measured by the #9774 harness in #9834/#9835' },
	{ veto: 'click-ratio', reason: 'measured by the #9774 harness in #9834/#9835' },
	{ veto: 'runaway-tail', reason: 'measured by the #9774 harness in #9834/#9835' },
	{ veto: 'cpu', reason: 'the #9777 budget row' }
];

export class VetoError extends Error {
	/** @param {{veto:string,file:string,kernel:string,axis?:string,candidate?:string,fixture:string,position:string,measured:number,limit:number}} f */
	constructor(f) {
		super(`veto ${f.veto} failed: ${f.file} kernel=${f.kernel}${f.axis ? ` axis=${f.axis}` : ''}${f.candidate != null ? ` candidate=${f.candidate}` : ''} fixture=${f.fixture} ${f.position} measured=${f.measured} limit=${f.limit}`);
		Object.assign(this, f);
		this.name = 'VetoError';
	}
}

export class UnavailableError extends Error {
	/** @param {string} reason @param {typeof REAL_PATH_DEPENDENCIES[number][]} missing */
	constructor(reason, missing) {
		super(reason);
		this.reason = reason;
		this.missing = missing;
		this.name = 'UnavailableError';
	}
}

export function resolveRealRenderer(repoRoot = REPO_ROOT) {
	const missing = REAL_PATH_DEPENDENCIES.filter((d) => !existsSync(join(repoRoot, d.path)));
	if (missing.length > 0) {
		const names = missing.map((d) => `${d.issue} ${d.what} (${d.path})`).join(', ');
		throw new UnavailableError(
			`UNAVAILABLE: flanger and phaser calibration renders only through mod-fx-processor.js in an OfflineAudioContext; missing ${names}`,
			missing
		);
	}
	throw new UnavailableError(
		'UNAVAILABLE: the #9774 harness and kernels are present but this script has no adapter yet; wire resolveRealRenderer to the #9774 harness (or the #10422 worklet host) running mod-fx-processor.js',
		[]
	);
}

//----------------------------------------------------------------------------- helpers
function _rng(seed) {
	let state = seed >>> 0 || 1;
	return () => {
		state ^= state << 13;
		state ^= state >>> 17;
		state ^= state << 5;
		return (state >>> 0) / 0x100000000;
	};
}
const _dbToLin = (db) => 10 ** (db / 20);
function _rms(channels) {
	let sum = 0;
	let n = 0;
	for (const ch of channels) for (const x of ch) { sum += x * x; n++; }
	return Math.sqrt(sum / n);
}

/** @param {typeof FIXTURES} fixtureDefs */
export function canonicalPairs(fixtureDefs = FIXTURES) {
	const entries = [];
	for (let ai = 0; ai < AXES.length; ai++) {
		const { kernel, axis, values } = AXES[ai];
		if (values.length === 3) {
			const pairings = [
				[values[0], values[1]],
				[values[0], values[2]],
				[values[1], values[2]]
			];
			pairings.forEach((pair, k) => {
				entries.push({ kernel, axis, x: pair[0], y: pair[1], fixture: fixtureDefs[(ai + k) % fixtureDefs.length].name });
			});
		} else if (values.length === 2) {
			for (const f of fixtureDefs) entries.push({ kernel, axis, x: values[0], y: values[1], fixture: f.name });
		}
	}
	return entries;
}

function _voicing(kernel, axis, value) {
	return { ...BASELINE[kernel], [axis]: value };
}

export function monoFoldLossDb(channels) {
	let sumLr = 0;
	let sumMono = 0;
	const n = channels[0].length;
	if (n === 0) return 0;
	let allZero = true;
	for (let i = 0; i < n; i++) {
		const l = channels[0][i];
		const r = channels[1][i];
		sumLr += l * l + r * r;
		const m = (l + r) * 0.5;
		sumMono += m * m;
		if (l !== 0 || r !== 0) allZero = false;
	}
	if (allZero) return 0;
	if (sumMono === 0) return Number.POSITIVE_INFINITY;
	return 10 * Math.log10(sumLr / (2 * sumMono));
}

function _biquadCoeffs(type, sr, f0, q, gainDb = 0) {
	const w0 = (2 * Math.PI * f0) / sr;
	const cosw = Math.cos(w0);
	const sinw = Math.sin(w0);
	const alpha = sinw / (2 * q);
	const A = 10 ** (gainDb / 40);
	let b0, b1, b2, a0, a1, a2;
	if (type === 'highshelf') {
		const sqrtA = Math.sqrt(A);
		const twoSqrtAalpha = 2 * sqrtA * alpha;
		b0 = A * (A + 1 + (A - 1) * cosw + twoSqrtAalpha);
		b1 = -2 * A * (A - 1 + (A + 1) * cosw);
		b2 = A * (A + 1 + (A - 1) * cosw - twoSqrtAalpha);
		a0 = A + 1 - (A - 1) * cosw + twoSqrtAalpha;
		a1 = 2 * (A - 1 - (A + 1) * cosw);
		a2 = A + 1 - (A - 1) * cosw - twoSqrtAalpha;
	} else if (type === 'highpass') {
		b0 = (1 + cosw) / 2;
		b1 = -(1 + cosw);
		b2 = (1 + cosw) / 2;
		a0 = 1 + alpha;
		a1 = -2 * cosw;
		a2 = 1 - alpha;
	} else throw new Error(`unknown biquad ${type}`);
	return { b0: b0 / a0, b1: b1 / a0, b2: b2 / a0, a1: a1 / a0, a2: a2 / a0 };
}
function _biquadStep(c, s, x) {
	const y = c.b0 * x + c.b1 * s.x1 + c.b2 * s.x2 - c.a1 * s.y1 - c.a2 * s.y2;
	s.x2 = s.x1;
	s.x1 = x;
	s.y2 = s.y1;
	s.y1 = y;
	return y;
}

/** ITU-R BS.1770-4 integrated loudness (two channels, equal weight). */
export function integratedLufs(channels, sr) {
	const hs = _biquadCoeffs('highshelf', sr, 1681.974450955533, 0.7071752369554196, 3.999843853973347);
	const hp = _biquadCoeffs('highpass', sr, 38.13547087602444, 0.5003270373238773);
	const n = channels[0].length;
	const filtered = channels.map((ch) => {
		const out = new Float64Array(n);
		const state = {
			hs: { x1: 0, x2: 0, y1: 0, y2: 0 },
			hp: { x1: 0, x2: 0, y1: 0, y2: 0 }
		};
		for (let i = 0; i < n; i++) {
			let x = ch[i];
			x = _biquadStep(hs, state.hs, x);
			x = _biquadStep(hp, state.hp, x);
			out[i] = x;
		}
		return out;
	});
	const block = Math.round(0.4 * sr);
	const hop = Math.round(0.1 * sr);
	const blockMs = [];
	for (let start = 0; start + block <= n; start += hop) {
		let sum = 0;
		for (let c = 0; c < 2; c++) {
			let chMs = 0;
			for (let i = start; i < start + block; i++) {
				const x = filtered[c][i];
				chMs += x * x;
			}
			sum += chMs / block;
		}
		blockMs.push(sum);
	}
	if (blockMs.length === 0) throw new Error('integratedLufs: no 400 ms blocks survived');
	const absGate = 10 ** (-70 / 10);
	const gated = blockMs.filter((z) => z >= absGate);
	if (gated.length === 0) throw new Error('integratedLufs: all blocks below absolute gate');
	const meanZ = gated.reduce((a, z) => a + z, 0) / gated.length;
	const lufs = -0.691 + 10 * Math.log10(meanZ);
	const relGate = 10 ** ((lufs - 10) / 10);
	const gated2 = gated.filter((z) => z >= relGate);
	if (gated2.length === 0) throw new Error('integratedLufs: all blocks below relative gate');
	const z = gated2.reduce((a, v) => a + v, 0) / gated2.length;
	return -0.691 + 10 * Math.log10(z);
}

export function lufsDeltaDb(wet, dry, sr) {
	return integratedLufs(wet, sr) - integratedLufs(dry, sr);
}

function _fft(re, im) {
	const n = re.length;
	for (let i = 1, j = 0; i < n; i++) {
		let bit = n >> 1;
		for (; j & bit; bit >>= 1) j ^= bit;
		j ^= bit;
		if (i < j) [re[i], re[j]] = [re[j], re[i]], [im[i], im[j]] = [im[j], im[i]];
	}
	for (let len = 2; len <= n; len <<= 1) {
		const ang = (-2 * Math.PI) / len;
		const wlenRe = Math.cos(ang);
		const wlenIm = Math.sin(ang);
		for (let i = 0; i < n; i += len) {
			let wRe = 1;
			let wIm = 0;
			for (let j = 0; j < len / 2; j++) {
				const uRe = re[i + j];
				const uIm = im[i + j];
				const vRe = re[i + j + len / 2] * wRe - im[i + j + len / 2] * wIm;
				const vIm = re[i + j + len / 2] * wIm + im[i + j + len / 2] * wRe;
				re[i + j] = uRe + vRe;
				im[i + j] = uIm + vIm;
				re[i + j + len / 2] = uRe - vRe;
				im[i + j + len / 2] = uIm - vIm;
				const nwRe = wRe * wlenRe - wIm * wlenIm;
				wIm = wRe * wlenIm + wIm * wlenRe;
				wRe = nwRe;
			}
		}
	}
}

function _bandRatiosDb(wet, dry, sampleRate) {
	const frame = 2048;
	const hop = 1024;
	const n = wet[0].length;
	const maxF = Math.min(10000, 0.45 * sampleRate);
	const bandEdges = [];
	for (let f = 100; f < maxF; f *= 2 ** (1 / 6)) bandEdges.push(f);
	bandEdges.push(maxF);
	const frameRatios = [];
	for (let start = 0; start + frame <= n; start += hop) {
		const re = new Float64Array(frame);
		const im = new Float64Array(frame);
		for (let i = 0; i < frame; i++) {
			const w = 0.5 * (1 - Math.cos((2 * Math.PI * i) / (frame - 1)));
			const v = w * ((wet[0][start + i] + wet[1][start + i]) * 0.5);
			re[i] = v;
		}
		_fft(re, im);
		const wetP = new Float64Array(bandEdges.length - 1);
		const dryP = new Float64Array(bandEdges.length - 1);
		for (let k = 1; k < frame / 2; k++) {
			const freq = (k * sampleRate) / frame;
			const p = re[k] * re[k] + im[k] * im[k];
			for (let b = 0; b < bandEdges.length - 1; b++) {
				if (freq >= bandEdges[b] && freq < bandEdges[b + 1]) {
					wetP[b] += p;
					break;
				}
			}
		}
		const reD = new Float64Array(frame);
		const imD = new Float64Array(frame);
		for (let i = 0; i < frame; i++) {
			const w = 0.5 * (1 - Math.cos((2 * Math.PI * i) / (frame - 1)));
			reD[i] = w * ((dry[0][start + i] + dry[1][start + i]) * 0.5);
		}
		_fft(reD, imD);
		for (let k = 1; k < frame / 2; k++) {
			const freq = (k * sampleRate) / frame;
			const p = reD[k] * reD[k] + imD[k] * imD[k];
			for (let b = 0; b < bandEdges.length - 1; b++) {
				if (freq >= bandEdges[b] && freq < bandEdges[b + 1]) {
					dryP[b] += p;
					break;
				}
			}
		}
		const ratios = [];
		for (let b = 0; b < wetP.length; b++) {
			if (dryP[b] > 0) ratios.push(10 * Math.log10(wetP[b] / dryP[b]));
		}
		if (ratios.length) frameRatios.push(ratios);
	}
	return frameRatios;
}

export function resonancePeakDb(wet, dry, sr) {
	const frames = _bandRatiosDb(wet, dry, sr);
	if (!frames.length) return 0;
	const perFrame = frames.map((r) => {
		const max = Math.max(...r);
		const med = r.slice().sort((a, b) => a - b)[Math.floor(r.length / 2)];
		return max - med;
	});
	perFrame.sort((a, b) => a - b);
	return perFrame[Math.floor(perFrame.length / 2)];
}

export function medianNotchDepthDb(wet, dry, sr) {
	const frames = _bandRatiosDb(wet, dry, sr);
	if (!frames.length) return 0;
	const perFrame = frames.map((r) => {
		const min = Math.min(...r);
		const med = r.slice().sort((a, b) => a - b)[Math.floor(r.length / 2)];
		return med - min;
	});
	perFrame.sort((a, b) => a - b);
	return perFrame[Math.floor(perFrame.length / 2)];
}

//----------------------------------------------------------------------------- fixtures
function _fixtureFilter(type, fc, q, sr) {
	if (!['bandpass', 'highpass'].includes(type)) throw new Error(`unknown fixture filter type ${type}`);
	const g = Math.tan((Math.PI * Math.min(fc, sr * 0.45)) / sr);
	const k = 1 / q;
	const a1 = 1 / (1 + g * (g + k));
	return { type, k, a1, a2: g * a1, a3: g * g * a1 };
}
const _fixtureFilterState = () => ({ ic1: 0, ic2: 0 });
function _fixtureFilterStep(c, s, x) {
	const v3 = x - s.ic2;
	const v1 = c.a1 * s.ic1 + c.a2 * v3;
	const v2 = s.ic2 + c.a2 * s.ic1 + c.a3 * v3;
	s.ic1 = 2 * v1 - s.ic1;
	s.ic2 = 2 * v2 - s.ic2;
	if (Math.abs(s.ic1) < DENORMAL_FLUSH) s.ic1 = 0;
	if (Math.abs(s.ic2) < DENORMAL_FLUSH) s.ic2 = 0;
	let y;
	if (c.type === 'bandpass') y = c.k * v1;
	else if (c.type === 'highpass') y = x - c.k * v1 - v2;
	return Math.abs(y) < DENORMAL_FLUSH ? 0 : y;
}

function _kick(buf, start, sr, gain) {
	const len = Math.round(0.5 * sr);
	let ph = 0;
	for (let j = 0; j < len && start + j < buf.length; j++) {
		const t = j / sr;
		ph += (2 * Math.PI * (50 + 100 * Math.exp(-t / 0.04))) / sr;
		buf[start + j] += gain * Math.sin(ph) * Math.exp(-t / 0.25);
	}
}
function _burst(buf, start, sr, seconds, gain, next, coeffs, toneHz = 0) {
	const len = Math.round(seconds * sr);
	const s = _fixtureFilterState();
	for (let j = 0; j < len && start + j < buf.length; j++) {
		const env = Math.exp(-j / (len / 4));
		const tone = toneHz ? 0.5 * Math.sin((2 * Math.PI * toneHz * j) / sr) : 0;
		buf[start + j] += gain * env * (_fixtureFilterStep(coeffs, s, next() * 2 - 1) + tone);
	}
}

function _houseLoop(n, sr, bpm, seed) {
	const channels = [new Float64Array(n), new Float64Array(n)];
	const beat = (60 / bpm) * sr;
	const hat = _fixtureFilter('highpass', 8000, Math.SQRT1_2, sr);
	const next = _rng(seed ^ 0xb001);
	let bassPh = 0;
	for (let i = 0; i < n; i++) {
		const bar = Math.floor(i / (beat * BEATS_PER_BAR));
		bassPh += (2 * Math.PI * 55) / sr;
		const bass = 0.35 * Math.sin(bassPh) * (bar % 2 === 0 ? 1 : 0.7);
		channels[0][i] += bass;
		channels[1][i] += bass;
	}
	for (let b = 0; b * beat < n; b++) {
		for (const c of channels) {
			_kick(c, Math.round(b * beat), sr, 0.9);
			_burst(c, Math.round((b + 0.5) * beat), sr, 0.03, 0.25, next, hat);
		}
	}
	return channels;
}

function _vocalPhrase(n, sr, bpm, seed) {
	const channels = [new Float64Array(n), new Float64Array(n)];
	const beat = (60 / bpm) * sr;
	const notes = [165, 196, 220, 247];
	let ph = 0;
	const voice = new Float64Array(n);
	for (let i = 0; i < n; i++) {
		const beatIdx = Math.floor(i / beat);
		const syllable = Math.floor(beatIdx / 2);
		const gap = beatIdx % 2 === 1 && (i % beat) / beat > 0.85;
		const f0 = notes[syllable % notes.length] * (1 + 0.02 * Math.sin((2 * Math.PI * 5 * i) / sr));
		ph += (2 * Math.PI * f0) / sr;
		let saw = 0;
		for (let h = 1; h <= 12; h++) saw += Math.sin(h * ph) / h;
		voice[i] = gap ? 0 : saw * Math.exp(-((i % beat) / beat) * 0.5);
	}
	const formants = [700, 1200].map((f) => {
		const c = _fixtureFilter('bandpass', f, 5, sr);
		const s = _fixtureFilterState();
		return Float64Array.from(voice, (x) => _fixtureFilterStep(c, s, x));
	});
	const vocal = Float64Array.from(formants[0], (x, i) => x + formants[1][i]);
	const g = _dbToLin(-6);
	for (let i = 0; i < n; i++) {
		channels[0][i] = vocal[i] * g;
		channels[1][i] = vocal[i] * g;
	}
	return channels;
}

function _hihatBreak(n, sr, bpm, seed) {
	const beat = (60 / bpm) * sr;
	const sixteenth = beat / 4;
	const hat = _fixtureFilter('highpass', 10000, Math.SQRT1_2, sr);
	const snr = _fixtureFilter('bandpass', 2000, 1, sr);
	const channels = [new Float64Array(n), new Float64Array(n)];
	const next = _rng(seed ^ 0xb102);
	const accent = [1, 0.6, 0.8, 0.5];
	for (let s = 0; s * sixteenth < n; s++) {
		const gain = 0.2 * accent[s % 4];
		for (const ch of channels) _burst(ch, Math.round(s * sixteenth), sr, 0.02, gain, next, hat);
	}
	for (let b = 0; b * beat < n; b++) {
		for (const ch of channels) {
			_kick(ch, Math.round(b * beat), sr, 0.85);
			_kick(ch, Math.round((b + 2.5) * beat), sr, 0.5);
			if (b % 2 === 1) _burst(ch, Math.round(b * beat), sr, 0.1, 0.55, next, snr, 200);
		}
	}
	return channels;
}

function _normaliseFixture(name, source, channels, bpm) {
	const gain = _dbToLin(DRY_RMS_DBFS) / _rms(channels);
	const scaled = channels.map((ch) => Float64Array.from(ch, (x) => x * gain));
	let peak = 0;
	for (const ch of scaled) for (const x of ch) peak = Math.max(peak, Math.abs(x));
	const peakDb = Math.round(2000 * Math.log10(peak || 1e-30)) / 100;
	if (peakDb > FIXTURE_PEAK_MAX_DBFS) {
		throw new Error(`fixture ${name} peaks at ${peakDb} dBFS after RMS normalisation to ${DRY_RMS_DBFS} dBFS, limit ${FIXTURE_PEAK_MAX_DBFS}`);
	}
	return { name, bpm, source, channels: scaled, dry_rms_dbfs: DRY_RMS_DBFS, peak_dbfs: peakDb };
}

function _buildFixtures({ bars, sampleRate, seed, overrides }) {
	const built = [];
	const synth = {
		'house-loop': (n, sr, s) => _houseLoop(n, sr, 124, s),
		'vocal-phrase': (n, sr, s) => _vocalPhrase(n, sr, 100, s),
		'hihat-break': (n, sr, s) => _hihatBreak(n, sr, 174, s)
	};
	const seeds = { 'house-loop': 0xb001, 'vocal-phrase': 0xb002, 'hihat-break': 0xb003 };
	for (const def of FIXTURES) {
		const secs = (bars * BEATS_PER_BAR * 60) / def.bpm;
		const n = Math.round(secs * sampleRate);
		const path = overrides?.[def.name];
		let channels;
		let source;
		if (path) {
			channels = _readWav(path, sampleRate, n);
			source = 'wav';
		} else {
			channels = synth[def.name](n, sampleRate, seed ^ seeds[def.name]);
			source = 'synthesized';
		}
		const norm = _normaliseFixture(def.name, source, channels, def.bpm);
		norm.seconds = Math.round(secs * 100) / 100;
		norm.bpm = def.bpm;
		built.push(norm);
	}
	return built;
}

function _pinkNoise(seconds, sampleRate, seed) {
	const n = Math.round(seconds * sampleRate);
	const next = _rng(seed ^ 0xb004);
	let b0 = 0; let b1 = 0; let b2 = 0; let b3 = 0; let b4 = 0; let b5 = 0; let b6 = 0;
	const mono = new Float64Array(n);
	for (let i = 0; i < n; i++) {
		const white = next() * 2 - 1;
		b0 = 0.99886 * b0 + white * 0.0555179;
		b1 = 0.99332 * b1 + white * 0.0750759;
		b2 = 0.969 * b2 + white * 0.153852;
		b3 = 0.8665 * b3 + white * 0.3104856;
		b4 = 0.55 * b4 + white * 0.5329522;
		b5 = -0.7616 * b5 - white * 0.016898;
		mono[i] = (b0 + b1 + b2 + b3 + b4 + b5 + b6 + white * 0.5362) * 0.11;
		b6 = white * 0.115926;
	}
	const channels = [mono, Float64Array.from(mono)];
	const gain = _dbToLin(DRY_RMS_DBFS) / _rms(channels);
	return channels.map((ch) => Float64Array.from(ch, (x) => x * gain));
}

//----------------------------------------------------------------------------- vetoes
function _veto(veto, ctx, measured, limit) {
	throw new VetoError({ veto, ...ctx, measured, limit });
}
function _checkFiniteAndPeak(channels, ctx) {
	for (const ch of channels) {
		for (const x of ch) {
			const f = Math.fround(x);
			if (!Number.isFinite(f) || (f !== 0 && Math.abs(f) < FLT_MIN_NORMAL)) _veto('finite', ctx, f, 'finite normal float32');
		}
	}
	for (const ch of channels) for (const x of ch) if (Math.abs(x) > 1) _veto('peak', ctx, Math.abs(x), 1);
}
function _checkShape(out, len) {
	if (!Array.isArray(out) || out.length !== 2 || out[0].length !== len || out[1].length !== len) {
		throw new Error(`render returned ${out?.length} channels of length ${out?.[0]?.length}, expected 2 x ${len}`);
	}
}

//----------------------------------------------------------------------------- manifest
/** When #9776 lands tools/fx-ab/manifest.schema.json, validate against that file here and keep these checks only for the meta block. */
export function assertPairsManifestShape(manifest) {
	const fail = (field) => { throw new Error(`pairs manifest shape: ${field}`); };
	if (typeof manifest?.family !== 'string' || !manifest.family) fail('family must be a non-empty string');
	if (!Number.isInteger(manifest.round) || manifest.round < 1) fail('round must be an integer >= 1');
	if (!Number.isInteger(manifest.seed) || manifest.seed < 0 || manifest.seed > 0xffffffff) fail('seed must be a uint32');
	if (!Array.isArray(manifest.pairs) || manifest.pairs.length === 0) fail('pairs must be a non-empty array');
	if (typeof manifest.renderer?.id !== 'string' || !manifest.renderer.id || typeof manifest.renderer.real_path !== 'boolean') fail('renderer must be {id: non-empty string, real_path: boolean}');
	if (!Array.isArray(manifest.hidden_fields) || !manifest.hidden_fields.includes('meta')) fail('hidden_fields must include meta');
	const ids = new Set();
	const fixtureNames = new Set((manifest.fixtures ?? []).map((f) => f.name));
	for (const p of manifest.pairs) {
		if (typeof p.id !== 'string' || ids.has(p.id)) fail(`pair id ${p.id} must be a unique string`);
		ids.add(p.id);
		if (typeof p.a !== 'string' || typeof p.b !== 'string' || p.a === p.b) fail(`pair ${p.id} a/b must be distinct strings`);
		if (typeof p.question !== 'string') fail(`pair ${p.id} question`);
		const m = p.meta;
		if (!m || !KERNELS.includes(m.kernel)) fail(`pair ${p.id} meta.kernel`);
		const ax = AXES.find((a) => a.kernel === m.kernel && a.axis === m.axis);
		if (!ax) fail(`pair ${p.id} meta.axis`);
		if (m.a_candidate === m.b_candidate) fail(`pair ${p.id} meta candidates must differ`);
		if (!ax.values.includes(m.a_candidate) || !ax.values.includes(m.b_candidate)) fail(`pair ${p.id} meta candidate values`);
		if (!fixtureNames.has(m.fixture)) fail(`pair ${p.id} meta.fixture`);
		for (const side of ['a', 'b']) {
			const px = m.proxies?.[side];
			if (!px || !Number.isFinite(px.resonance_peak_db) || !Number.isFinite(px.median_notch_depth_db)) fail(`pair ${p.id} proxies.${side}`);
			for (const d of PROXY_DEPTHS) {
				if (!Number.isFinite(px.lufs_delta?.[String(d)])) fail(`pair ${p.id} proxies.${side}.lufs_delta.${d}`);
			}
		}
	}
}

//----------------------------------------------------------------------------- WAV IO
function _encodeWav(channels, sr) {
	const n = channels[0].length;
	const buf = Buffer.alloc(44 + n * 4);
	buf.write('RIFF', 0); buf.writeUInt32LE(36 + n * 4, 4); buf.write('WAVE', 8);
	buf.write('fmt ', 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(2, 22);
	buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * 4, 28); buf.writeUInt16LE(4, 32); buf.writeUInt16LE(16, 34);
	buf.write('data', 36); buf.writeUInt32LE(n * 4, 40);
	for (let i = 0; i < n; i++) {
		for (let c = 0; c < 2; c++) buf.writeInt16LE(Math.round(Math.max(-1, Math.min(1, channels[c][i])) * 32767), 44 + i * 4 + c * 2);
	}
	return buf;
}
export function _readWav(path, sr, n) {
	const buf = readFileSync(path);
	if (buf.toString('ascii', 0, 4) !== 'RIFF' || buf.toString('ascii', 8, 12) !== 'WAVE') throw new Error(`${path} is not RIFF/WAVE`);
	let off = 12;
	let fmt = null;
	while (off + 8 <= buf.length) {
		const id = buf.toString('ascii', off, off + 4);
		const size = buf.readUInt32LE(off + 4);
		if (id === 'fmt ') fmt = { format: buf.readUInt16LE(off + 8), ch: buf.readUInt16LE(off + 10), rate: buf.readUInt32LE(off + 12), bits: buf.readUInt16LE(off + 22) };
		else if (id === 'data') {
			if (!fmt) throw new Error(`${path} has data before fmt`);
			const kind = `${fmt.format}/${fmt.bits}`;
			if (!['1/16', '1/24', '3/32'].includes(kind) || fmt.ch < 1 || fmt.ch > 2) throw new Error(`${path} format ${kind} with ${fmt.ch} channels is unsupported (16/24-bit PCM or 32-bit float, mono or stereo)`);
			if (fmt.rate !== sr) throw new Error(`${path} sample rate ${fmt.rate} != --sample-rate ${sr}`);
			const bytes = fmt.bits / 8;
			const frames = Math.floor(size / (bytes * fmt.ch));
			if (frames < n) throw new Error(`${path} has ${frames} frames, needs ${n}`);
			const read = (p) => (kind === '1/16' ? buf.readInt16LE(p) / 32768 : kind === '1/24' ? buf.readIntLE(p, 3) / 8388608 : buf.readFloatLE(p));
			return [0, 1].map((c) => Float64Array.from({ length: n }, (_, i) => read(off + 8 + (i * fmt.ch + Math.min(c, fmt.ch - 1)) * bytes)));
		}
		off += 8 + size + (size % 2);
	}
	throw new Error(`${path} has no data chunk`);
}

//----------------------------------------------------------------------------- export
/**
 * @param {{outDir:string, renderer:Renderer, allowTestDouble?:boolean, seed?:number, bars?:number, sampleRate?:number, fixtures?:Record<string,string>}} opts
 */
export async function exportModFxRatingPairs({ outDir, renderer, allowTestDouble = false, seed = DEFAULT_SEED, bars = BARS, sampleRate = SAMPLE_RATE, fixtures }) {
	if (typeof renderer?.id !== 'string' || typeof renderer.realPath !== 'boolean' || typeof renderer.render !== 'function') throw new Error('renderer {id, realPath, render} is required');
	if (renderer.realPath !== true && !allowTestDouble) {
		throw new Error(`renderer ${renderer.id} is not a real-path renderer; a calibration export needs mod-fx-processor.js via the #9774 harness (pass allowTestDouble only from tests)`);
	}
	if (!outDir) throw new Error('--out <dir> is required');
	if (existsSync(outDir) && readdirSync(outDir).length > 0) throw new Error(`--out ${outDir} is not empty; refusing to overwrite an earlier rating round`);
	seed >>>= 0;
	const sr = sampleRate;
	const fx = _buildFixtures({ bars, sampleRate: sr, seed, overrides: fixtures });
	const fxByName = Object.fromEntries(fx.map((f) => [f.name, f]));
	const pinkSecs = (bars * BEATS_PER_BAR * 60) / PINK_BPM;
	const pink = _pinkNoise(pinkSecs, sr, seed ^ 0xb005);
	const pinkDry = pink;

	const cache = new Map();
	const renderCount = { n: 0 };

	const doRender = async (kernel, voicing, depth, input, bpm, inputName, file, axis, candidate) => {
		const key = `${kernel}|${JSON.stringify(voicing)}|${inputName}|${depth}`;
		if (cache.has(key)) return cache.get(key);
		renderCount.n++;
		const req = { kernel, voicing, depth, input, sampleRate: sr, bpm, firstDownbeatSec: 0 };
		const out = await renderer.render(req);
		_checkShape(out, input[0].length);
		const ctx = {
			file,
			kernel,
			axis: axis ?? '',
			candidate: candidate ?? '',
			fixture: inputName,
			position: `depth=${depth}`
		};
		_checkFiniteAndPeak(out, ctx);
		const fold = monoFoldLossDb(out);
		if (fold > MONO_FOLD_MAX_DB) _veto('mono-fold', ctx, fold, MONO_FOLD_MAX_DB);
		cache.set(key, out);
		return out;
	};

	const canonical = canonicalPairs(FIXTURES);
	const next = _rng(seed);
	for (const c of canonical) c.swap = next() < 0.5;
	for (let i = canonical.length - 1; i > 0; i--) {
		const j = Math.floor(next() * (i + 1));
		[canonical[i], canonical[j]] = [canonical[j], canonical[i]];
	}

	const clipEntries = [];
	const pairs = [];

	for (let idx = 0; idx < canonical.length; idx++) {
		const c = canonical[idx];
		const id = `p${String(idx + 1).padStart(2, '0')}`;
		const fixture = fxByName[c.fixture];
		const dry = fixture.channels;
		const [aVal, bVal] = c.swap ? [c.y, c.x] : [c.x, c.y];
		const aVoicing = _voicing(c.kernel, c.axis, aVal);
		const bVoicing = _voicing(c.kernel, c.axis, bVal);

		const aClip = await doRender(c.kernel, aVoicing, PAIR_DEPTH, dry, fixture.bpm, c.fixture, `clips/${id}-a.wav`, c.axis, aVal);
		const bClip = await doRender(c.kernel, bVoicing, PAIR_DEPTH, dry, fixture.bpm, c.fixture, `clips/${id}-b.wav`, c.axis, bVal);
		clipEntries.push([`clips/${id}-a.wav`, aClip], [`clips/${id}-b.wav`, bClip]);

		const buildProxies = async (voicing, clipWet, val) => {
			const pink1 = await doRender(c.kernel, voicing, 1, pink, PINK_BPM, 'pink', `proxy:${c.kernel}:${c.axis}=${val}:pink:depth=1`, c.axis, val);
			const pinkPair = await doRender(c.kernel, voicing, PAIR_DEPTH, pink, PINK_BPM, 'pink', `proxy:${c.kernel}:${c.axis}=${val}:pink:depth=0.5`, c.axis, val);
			const lufs = {};
			for (const d of PROXY_DEPTHS) {
				const wet = d === PAIR_DEPTH
					? clipWet
					: await doRender(c.kernel, voicing, d, dry, fixture.bpm, c.fixture, `proxy:${c.kernel}:${c.axis}=${val}:${c.fixture}:depth=${d}`, c.axis, val);
				lufs[String(d)] = Math.round(100 * lufsDeltaDb(wet, dry, sr)) / 100;
			}
			return {
				resonance_peak_db: Math.round(100 * resonancePeakDb(pink1, pinkDry, sr)) / 100,
				median_notch_depth_db: Math.round(100 * medianNotchDepthDb(pinkPair, pinkDry, sr)) / 100,
				lufs_delta: lufs
			};
		};

		const aProxies = await buildProxies(aVoicing, aClip, aVal);
		const bProxies = await buildProxies(bVoicing, bClip, bVal);

		pairs.push({
			id,
			a: `clips/${id}-a.wav`,
			b: `clips/${id}-b.wav`,
			question: QUESTION,
			meta: {
				kernel: c.kernel,
				axis: c.axis,
				fixture: c.fixture,
				bpm: fixture.bpm,
				depth: PAIR_DEPTH,
				a_candidate: aVal,
				b_candidate: bVal,
				a_voicing: aVoicing,
				b_voicing: bVoicing,
				proxies: { a: aProxies, b: bProxies }
			}
		});
	}

	const manifest = {
		family: 'mod-fx',
		round: 1,
		seed,
		generated_by: 'scripts/export-mod-fx-rating-pairs.mjs',
		renderer: { id: renderer.id, real_path: renderer.realPath },
		sample_rate: sr,
		bars,
		pair_depth: PAIR_DEPTH,
		proxy_depths: PROXY_DEPTHS,
		pink_bpm: PINK_BPM,
		fixtures: fx.map(({ name, bpm, source, seconds, dry_rms_dbfs, peak_dbfs }) => ({ name, bpm, source, seconds, dry_rms_dbfs, peak_dbfs })),
		baseline: BASELINE,
		axes: AXES,
		vetoes_applied: ['finite', 'peak', 'mono-fold'],
		vetoes_not_applied: VETOES_NOT_APPLIED,
		hidden_fields: ['meta'],
		pairs
	};
	assertPairsManifestShape(manifest);

	mkdirSync(join(outDir, 'clips'), { recursive: true });
	for (const [rel, channels] of clipEntries) writeFileSync(join(outDir, rel), _encodeWav(channels, sr));
	writeFileSync(join(outDir, 'pairs.json'), `${JSON.stringify(manifest, null, 2)}\n`);
	return { ...manifest, _render_call_count: renderCount.n };
}

//----------------------------------------------------------------------------- CLI
function _parseArgs(argv) {
	const opts = { fixtures: {} };
	for (let i = 0; i < argv.length; i++) {
		const flag = argv[i];
		const val = argv[++i];
		if (val === undefined) throw new Error(`${flag} needs a value`);
		if (flag === '--out') opts.outDir = val;
		else if (flag === '--seed') opts.seed = Number(val);
		else if (flag === '--bars') opts.bars = Number(val);
		else if (flag === '--sample-rate') opts.sampleRate = Number(val);
		else if (flag === '--fixture') {
			const eq = val.indexOf('=');
			const name = val.slice(0, eq);
			const path = val.slice(eq + 1);
			if (eq < 1 || !path) throw new Error(`--fixture expects <name>=<path.wav>, got ${val}`);
			opts.fixtures[name] = path;
		} else throw new Error(`unknown argument ${flag}`);
	}
	for (const k of ['seed', 'bars', 'sampleRate']) if (k in opts && !(opts[k] > 0 || (k === 'seed' && opts[k] === 0))) throw new Error(`invalid --${k}`);
	if (!opts.outDir) throw new Error('--out <dir> is required');
	return opts;
}
export async function main(argv) {
	let opts;
	try {
		opts = _parseArgs(argv);
	} catch (err) {
		console.error(`[ERROR] ${err.message}`);
		return 1;
	}
	let renderer;
	try {
		renderer = resolveRealRenderer();
	} catch (err) {
		if (!(err instanceof UnavailableError)) throw err;
		console.error(`[UNAVAILABLE] ${err.message}`);
		return EXIT_UNAVAILABLE;
	}
	try {
		const m = await exportModFxRatingPairs({ ...opts, renderer });
		console.log(`[OK] wrote ${m.pairs.length} pairs, ${m.pairs.length * 2} clips, manifest ${join(opts.outDir, 'pairs.json')}, seed ${m.seed}`);
		return 0;
	} catch (err) {
		if (err instanceof VetoError) console.error(`[VETO] ${err.file}: ${err.message}`);
		else console.error(`[ERROR] ${err.message}`);
		return 1;
	}
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
	process.exitCode = await main(process.argv.slice(2));
}
