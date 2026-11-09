#!/usr/bin/env node
/**
 * Export blind A/B pairs for SWEEP, NOISE and CRUSH sound color voicing (#9807, SNDCOL-05).
 *
 * Spec: (private path) sections 4.2 and 6.
 *
 * Calibration clips render ONLY through a real-path renderer: the #9774 fx render harness
 * driving the shipped #9803 SWEEP, #9804 NOISE and #9805 CRUSH kernels. Until those land and
 * an adapter is wired into resolveRealRenderer, the CLI prints [UNAVAILABLE], exits 2 and
 * writes nothing. This script holds no DSP of its own; the pairing, blinding, manifest,
 * vetoes and WAV writer run behind the Renderer interface below.
 *
 * Usage:
 *   node scripts/export-sound-color-pairs.mjs --out <dir> [--seed <uint32>] [--seconds <n>]
 *       [--sample-rate <hz>] [--fixture <name>=<path.wav>]...
 *
 * Mini-PRD:
 *   ✔︎ ✅ 🎯 R0 no real render path means UNAVAILABLE, never a calibration from a reimplementation
 *     [if] any real-path dependency file is missing [then ⛔️ unless the CLI exits 2 with [UNAVAILABLE] naming each missing issue and path]
 *     [if] UNAVAILABLE [then ⛔️ if --out was created or written]
 *     [if] exportSoundColorPairs gets a renderer with realPath !== true and no allowTestDouble [then ⛔️ unless it throws]
 *   ✔︎ ✅ R1 writes WAVs plus pairs.json (#9776 shape) to --out: 18 pairs, sides and order from a recorded seed
 *     [if] same seed twice [then ⛔️ if manifests differ]
 *     [if] two pairs share kernel, color and variant set [then ⛔️]
 *     [if] --out is missing or non-empty [then ⛔️ unless the CLI exits 1 naming it]
 *   ✔︎ ✅ R2 each pair records kernel, variants, COLOR, PARAMETER and RMS-dB-vs-dry at the four proxy positions
 *     [if] a loudness value is missing or non-finite [then ⛔️]
 *     [if] a variant name is not a key of variants[kernel] [then ⛔️]
 *     [if] NOISE deltas are not ordered by gainMaxDb [then ⛔️]
 *   ✔︎ ✅ R3 every render passes the section 6 hard vetoes before any file is written
 *     [if] a render exceeds 0 dBFS [then ⛔️ unless VetoError peak and an empty out dir]
 *     [if] a render holds NaN [then ⛔️ unless VetoError finite]
 *     [if] the detent is not bit-exact dry [then ⛔️]
 *   (R1-R3 are exercised under a test double only; they carry no real-path evidence yet.)
 */
import { mkdirSync, readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

/**
 * @typedef {[sampleIndex: number, value: number][]} Breakpoints  piecewise-linear COLOR automation, first at 0,
 *   held after the last point (what an OfflineAudioContext AudioParam ramp takes; serialisable into a page)
 * @typedef {{ kernel: 'SWEEP'|'NOISE'|'CRUSH', constants: object, input: Float64Array[], sampleRate: number,
 *   seed: number, parameter: number, color: Breakpoints }} RenderRequest
 * @typedef {{ id: string, realPath: boolean, render(req: RenderRequest): Promise<Float64Array[]> }} Renderer
 */

//----------------------------------------------------------------------------- CFG
export const DEFAULT_SEED = 0x5eed9807;
export const CLIP_SECONDS = 8;
export const SAMPLE_RATE = 48000;
export const PARAMETER = 0.5;
export const PAIR_POSITIONS = [0.15, 0.85];
export const PROXY_POSITIONS = [0.15, 0.3, 0.7, 0.85];
export const KERNELS = ['SWEEP', 'NOISE', 'CRUSH'];
export const DRY_RMS_DBFS = -23;
export const FIXTURE_PEAK_MAX_DBFS = -3;
export const QUESTION = 'Which sounds better as a DJ color effect?';
export const EXIT_UNAVAILABLE = 2;
export const REAL_PATH_DEPENDENCIES = Object.freeze([
	{ issue: '#9774', what: 'fx render harness', path: 'apps/webui/frontend/tests/e2e/support/fx-render-harness.ts' },
	{ issue: '#9803', what: 'SWEEP kernel', path: 'apps/webui/frontend/src/lib/rb/color-fx-sweep.ts' },
	{ issue: '#9804', what: 'NOISE kernel', path: 'apps/webui/frontend/src/lib/rb/color-fx-noise.ts' },
	{ issue: '#9805', what: 'CRUSH kernel', path: 'apps/webui/frontend/src/lib/rb/color-fx-crush.ts' }
]);
const REPO_ROOT = fileURLToPath(new URL('..', import.meta.url));
const RING_DOWN_LIMIT = 10 ** (-90 / 20);
const FLT_MIN_NORMAL = 1.1754943508222875e-38;
const DENORMAL_FLUSH = 1e-30;
const VARIANT_PAIRS = [
	['baseline', 'gentle'],
	['baseline', 'hard'],
	['gentle', 'hard']
];

/** Baseline = PR #492 constants (branch af--color-fx--rack, color-fx/constants.ts). */
export const VARIANTS = Object.freeze({
	SWEEP: {
		baseline: { kMin: 0.1, kMax: 2.0, cwMakeupDb: 0 },
		gentle: { kMin: 0.3, kMax: 1.4, cwMakeupDb: 1.5 },
		hard: { kMin: 0.05, kMax: 3.0, cwMakeupDb: 0 }
	},
	NOISE: {
		baseline: { gainMaxDb: -6 },
		gentle: { gainMaxDb: -12 },
		hard: { gainMaxDb: -4 }
	},
	CRUSH: {
		baseline: { taper: 'exponential', cwFixedAmount: 0.6 },
		gentle: { taper: 'late', cwFixedAmount: 0.4 },
		hard: { taper: 'steep', cwFixedAmount: 0.8 }
	}
});

const VETOES_NOT_APPLIED = [
	{ veto: 'added-latency', reason: 'measured by the #9774 harness in each kernel\'s own PR' },
	{ veto: 'click-ratio', reason: 'measured by the #9774 harness in each kernel\'s own PR' },
	{ veto: 'noise-seeded', reason: 'a property of the #9804 kernel source (seeded in-repo generator, no audio file, no Math.random), asserted in that PR' },
	{ veto: 'noise-decorrelated', reason: 'a property of the #9804 kernel source (seeded in-repo generator, no audio file, no Math.random), asserted in that PR' }
];

export class VetoError extends Error {
	/** @param {{veto:string,kernel:string,variant:string,fixture:string,position:string,measured:number,limit:number}} f */
	constructor(f) {
		super(`veto ${f.veto} failed: kernel=${f.kernel} variant=${f.variant} fixture=${f.fixture} ${f.position} measured=${f.measured} limit=${f.limit}`);
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

//----------------------------------------------------------------------------- real render path
/** The only source of a calibration renderer. Throws UnavailableError until the harness adapter exists. */
export function resolveRealRenderer(repoRoot = REPO_ROOT) {
	const missing = REAL_PATH_DEPENDENCIES.filter((d) => !existsSync(join(repoRoot, d.path)));
	if (missing.length > 0) {
		const names = missing.map((d) => `${d.issue} ${d.what} (${d.path})`).join(', ');
		throw new UnavailableError(`UNAVAILABLE: sound color calibration renders only through the real kernels; missing ${names}`, missing);
	}
	throw new UnavailableError('UNAVAILABLE: the #9774 harness and kernels are present but this script has no adapter to them yet; wire resolveRealRenderer to renderEffect', []);
}

//----------------------------------------------------------------------------- helpers
/** Seeded xorshift32, same algorithm as color-fx-graph.ts reverbImpulse. Returns [0, 1). */
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
const _ratioDb = (num, den) => Math.round(2000 * Math.log10(_rms(num) / _rms(den))) / 100 + 0; // + 0 turns -0 into 0
/** COLOR at sample i from piecewise-linear breakpoints; holds the last value. */
export function colorAtSample(breakpoints, i) {
	if (i <= breakpoints[0][0]) return breakpoints[0][1];
	for (let k = 1; k < breakpoints.length; k++) {
		const [i1, v1] = breakpoints[k];
		if (i <= i1) {
			const [i0, v0] = breakpoints[k - 1];
			return i1 === i0 ? v1 : v0 + ((v1 - v0) * (i - i0)) / (i1 - i0);
		}
	}
	return breakpoints[breakpoints.length - 1][1];
}

//----------------------------------------------------------------------------- fixtures
// _fixtureFilter* synthesize fixture INPUT only (hats, snare, formants); never on a render path.
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
function _drumBus(n, sr, bpm, seed, { snare, sixteenthHats }) {
	const channels = [new Float64Array(n), new Float64Array(n)];
	const beat = (60 / bpm) * sr;
	const hat = _fixtureFilter('highpass', 8000, Math.SQRT1_2, sr);
	const snr = _fixtureFilter('bandpass', 2000, 1, sr);
	channels.forEach((buf, c) => {
		const next = _rng(seed ^ (0x1000 + c));
		for (let b = 0; b * beat < n; b++) {
			_kick(buf, Math.round(b * beat), sr, 1);
			if (snare && b % 2 === 1) _burst(buf, Math.round(b * beat), sr, 0.12, 0.6, next, snr, 180);
			const hats = sixteenthHats ? [0.25, 0.5, 0.75] : [0.5];
			for (const h of hats) _burst(buf, Math.round((b + h) * beat), sr, 0.03, 0.3, next, hat);
		}
	});
	return channels;
}
function _houseVocal(n, sr, seed) {
	const bpm = 124;
	const beat = (60 / bpm) * sr;
	const drums = _drumBus(n, sr, bpm, seed, { snare: false, sixteenthHats: false });
	const voice = new Float64Array(n);
	const notes = [220, 246.94, 261.63, 196];
	let ph = 0;
	let bassPh = 0;
	for (let i = 0; i < n; i++) {
		const f0 = notes[Math.floor(i / (2 * beat)) % notes.length] * 2 ** ((20 / 1200) * Math.sin((2 * Math.PI * 5 * i) / sr));
		ph += (2 * Math.PI * f0) / sr;
		let saw = 0;
		for (let h = 1; h <= 12; h++) saw += Math.sin(h * ph) / h;
		voice[i] = saw;
		const offbeat = (i / beat) % 1 >= 0.5;
		bassPh += (2 * Math.PI * 55) / sr;
		drums[0][i] += offbeat ? 0.4 * Math.sin(bassPh) : 0;
		drums[1][i] += offbeat ? 0.4 * Math.sin(bassPh) : 0;
	}
	const formants = [700, 1200].map((f) => {
		const c = _fixtureFilter('bandpass', f, 5, sr);
		const s = _fixtureFilterState();
		return Float64Array.from(voice, (x) => _fixtureFilterStep(c, s, x));
	});
	const vocal = Float64Array.from(formants[0], (x, i) => x + formants[1][i]);
	const vocalGain = (_rms([drums[0]]) * _dbToLin(3)) / _rms([vocal]);
	return drums.map((ch) => Float64Array.from(ch, (x, i) => x + vocalGain * vocal[i]));
}
function _normaliseFixture(name, source, channels) {
	const gain = _dbToLin(DRY_RMS_DBFS) / _rms(channels);
	const scaled = channels.map((ch) => Float64Array.from(ch, (x) => x * gain));
	let peak = 0;
	for (const ch of scaled) for (const x of ch) peak = Math.max(peak, Math.abs(x));
	const peakDb = Math.round(2000 * Math.log10(peak)) / 100;
	if (peakDb > FIXTURE_PEAK_MAX_DBFS) {
		throw new Error(`fixture ${name} peaks at ${peakDb} dBFS after RMS normalisation to ${DRY_RMS_DBFS} dBFS, limit ${FIXTURE_PEAK_MAX_DBFS}`);
	}
	return { name, source, channels: scaled, dry_rms_dbfs: DRY_RMS_DBFS, peak_dbfs: peakDb };
}
function _buildFixtures(seconds, sr, seed, overrides) {
	const n = Math.round(seconds * sr);
	const synth = {
		'house-vocal': () => _houseVocal(n, sr, seed),
		drums: () => _drumBus(n, sr, 128, seed ^ 0xd2, { snare: true, sixteenthHats: true })
	};
	return Object.keys(synth).map((name) => {
		const path = overrides?.[name];
		if (path) return _normaliseFixture(name, 'wav', _readWav(path, sr, n));
		return _normaliseFixture(name, 'synthesized', synth[name]());
	});
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
function _maxDiff(a, b, from = 0) {
	let m = 0;
	for (let c = 0; c < 2; c++) for (let i = from; i < a[c].length; i++) m = Math.max(m, Math.abs(a[c][i] - b[c][i]));
	return m;
}
/** Runs every applicable section 6 veto on one kernel x variant x fixture, and returns proxy renders. */
async function _renderAndVeto(renderer, kernel, variantName, constants, fixture, sr, seed) {
	const dry = fixture.channels;
	const n = dry[0].length;
	const silent = [new Float64Array(n), new Float64Array(n)];
	const base = { kernel, variant: variantName, fixture: fixture.name };
	const render = (color, parameter, input = dry) => renderer.render({ kernel, constants, input, sampleRate: sr, seed, parameter, color });
	const proxies = {};
	for (const color of PROXY_POSITIONS) {
		const position = `color=${color} parameter=${PARAMETER}`;
		const out = await render([[0, color]], PARAMETER);
		_checkFiniteAndPeak(out, { ...base, position });
		const proxy = { out, noiseAlone: null };
		if (kernel === 'NOISE') {
			proxy.noiseAlone = await render([[0, color]], PARAMETER, silent);
			_checkFiniteAndPeak(proxy.noiseAlone, { ...base, position: `${position} noise-alone` });
		}
		proxies[color] = proxy;
	}
	for (const color of [0, 0.15, 0.85, 1]) {
		for (const param of [0, 1]) {
			_checkFiniteAndPeak(await render([[0, color]], param), { ...base, position: `color=${color} parameter=${param}` });
		}
	}
	const sweepStart = Math.round(0.1 * sr);
	const sweepLen = Math.round(0.1 * sr);
	const sweepColor = [
		[0, 0.5],
		[sweepStart - 1, 0.5],
		[sweepStart, 0],
		[sweepStart + sweepLen / 2, 1],
		[sweepStart + sweepLen - 1, 0],
		[sweepStart + sweepLen, 0.5]
	];
	for (const param of [0, 1]) {
		const ctx = { ...base, position: `fast-sweep parameter=${param}` };
		const out = await render(sweepColor, param);
		_checkFiniteAndPeak(out, ctx);
		const ring = _maxDiff(out, dry, sweepStart + sweepLen + Math.round(0.05 * sr));
		if (ring > RING_DOWN_LIMIT) _veto('ring-down', ctx, ring, RING_DOWN_LIMIT);
	}
	for (const param of [0, 0.5, 1]) {
		const d = _maxDiff(await render([[0, 0.5]], param), dry);
		if (d !== 0) _veto('detent-bit-exact', { ...base, position: `color=0.5 parameter=${param}` }, d, 0);
	}
	if (kernel === 'CRUSH') {
		const leftOnly = [dry[0], new Float64Array(n)];
		for (const color of PAIR_POSITIONS) {
			const right = (await render([[0, color]], PARAMETER, leftOnly))[1];
			const m = right.reduce((acc, x) => Math.max(acc, Math.abs(x)), 0);
			if (m !== 0) _veto('crush-stereo-independent', { ...base, position: `color=${color} left-only` }, m, 0);
		}
	}
	return proxies;
}

//----------------------------------------------------------------------------- pairs and manifest
function _loudness(kernel, proxy, dry) {
	return kernel === 'NOISE' ? _ratioDb(proxy.noiseAlone, dry) : _ratioDb(proxy.out, dry);
}
/** @param {any} manifest */
export function assertPairsManifestShape(manifest) {
	const fail = (field) => { throw new Error(`pairs manifest shape: ${field}`); };
	if (typeof manifest?.family !== 'string' || !manifest.family) fail('family must be a non-empty string');
	if (!Number.isInteger(manifest.round) || manifest.round < 1) fail('round must be an integer >= 1');
	if (!Number.isInteger(manifest.seed) || manifest.seed < 0 || manifest.seed > 0xffffffff) fail('seed must be a uint32');
	if (!Array.isArray(manifest.pairs) || manifest.pairs.length === 0) fail('pairs must be a non-empty array');
	if (typeof manifest.renderer?.id !== 'string' || !manifest.renderer.id || typeof manifest.renderer.real_path !== 'boolean') fail('renderer must be {id: non-empty string, real_path: boolean}');
	const ids = new Set();
	for (const p of manifest.pairs) {
		if (typeof p.id !== 'string' || ids.has(p.id)) fail(`pair id ${p.id} must be a unique string`);
		ids.add(p.id);
		if (typeof p.a !== 'string' || typeof p.b !== 'string' || p.a === p.b) fail(`pair ${p.id} a/b must be distinct strings`);
		if (typeof p.question !== 'string') fail(`pair ${p.id} question`);
		const m = p.meta;
		if (!m || !KERNELS.includes(m.kernel)) fail(`pair ${p.id} meta.kernel`);
		if (!PAIR_POSITIONS.includes(m.color)) fail(`pair ${p.id} meta.color`);
		if (m.parameter !== PARAMETER) fail(`pair ${p.id} meta.parameter`);
		const known = manifest.variants?.[m.kernel] ?? {};
		if (m.a_variant === m.b_variant || !(m.a_variant in known) || !(m.b_variant in known)) fail(`pair ${p.id} meta.a_variant/b_variant`);
		for (const name of [m.a_variant, m.b_variant]) {
			for (const pos of PROXY_POSITIONS) {
				if (!Number.isFinite(m.loudness_delta_db?.[name]?.[String(pos)])) fail(`pair ${p.id} meta.loudness_delta_db.${name}.${pos}`);
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
function _readWav(path, sr, n) {
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
 * @param {{outDir:string, renderer:Renderer, allowTestDouble?:boolean, seed?:number, seconds?:number, sampleRate?:number, fixtures?:Record<string,string>, variants?:any}} opts
 * @returns {Promise<any>} the manifest written to <outDir>/pairs.json
 */
export async function exportSoundColorPairs({ outDir, renderer, allowTestDouble = false, seed = DEFAULT_SEED, seconds = CLIP_SECONDS, sampleRate = SAMPLE_RATE, fixtures, variants = VARIANTS }) {
	if (typeof renderer?.id !== 'string' || typeof renderer.realPath !== 'boolean' || typeof renderer.render !== 'function') throw new Error('renderer {id, realPath, render} is required');
	if (renderer.realPath !== true && !allowTestDouble) throw new Error(`renderer ${renderer.id} is not a real-path renderer; a calibration export needs the #9774 harness (pass allowTestDouble only from tests)`);
	if (!outDir) throw new Error('--out <dir> is required');
	if (existsSync(outDir) && readdirSync(outDir).length > 0) throw new Error(`--out ${outDir} is not empty; refusing to overwrite an earlier rating round`);
	seed >>>= 0;
	const sr = sampleRate;
	const fx = _buildFixtures(seconds, sr, seed, fixtures);

	// Render and veto everything in memory before writing anything.
	const renders = {};
	for (const kernel of KERNELS) {
		renders[kernel] = {};
		for (const [name, v] of Object.entries(variants[kernel])) {
			renders[kernel][name] = {};
			for (const f of fx) renders[kernel][name][f.name] = await _renderAndVeto(renderer, kernel, name, v, f, sr, seed);
		}
	}

	const canonical = [];
	for (const kernel of KERNELS) {
		VARIANT_PAIRS.forEach(([x, y], vpi) => {
			PAIR_POSITIONS.forEach((color, pi) => canonical.push({ kernel, x, y, color, fixture: fx[(vpi + pi) % fx.length] }));
		});
	}
	const next = _rng(seed);
	for (const c of canonical) c.swap = next() < 0.5;
	for (let i = canonical.length - 1; i > 0; i--) {
		const j = Math.floor(next() * (i + 1));
		[canonical[i], canonical[j]] = [canonical[j], canonical[i]];
	}

	const clips = [];
	const pairs = canonical.map((c, idx) => {
		const id = `p${String(idx + 1).padStart(2, '0')}`;
		const [aVar, bVar] = c.swap ? [c.y, c.x] : [c.x, c.y];
		const deltas = {};
		for (const name of [aVar, bVar]) {
			deltas[name] = Object.fromEntries(PROXY_POSITIONS.map((pos) => [String(pos), _loudness(c.kernel, renders[c.kernel][name][c.fixture.name][pos], c.fixture.channels)]));
		}
		const a = `clips/${id}-a.wav`;
		const b = `clips/${id}-b.wav`;
		clips.push([a, renders[c.kernel][aVar][c.fixture.name][c.color].out], [b, renders[c.kernel][bVar][c.fixture.name][c.color].out]);
		return {
			id, a, b, question: QUESTION,
			meta: {
				kernel: c.kernel, fixture: c.fixture.name, color: c.color, parameter: PARAMETER,
				a_variant: aVar, b_variant: bVar, loudness_delta_db: deltas,
				metric: c.kernel === 'NOISE' ? 'noise_to_programme_db' : 'rms_db_vs_dry'
			}
		};
	});

	const manifest = {
		family: 'sound-color', round: 1, seed,
		generated_by: 'scripts/export-sound-color-pairs.mjs', renderer: { id: renderer.id, real_path: renderer.realPath },
		sample_rate: sr, seconds, parameter: PARAMETER, proxy_positions: PROXY_POSITIONS,
		fixtures: fx.map(({ name, source, dry_rms_dbfs, peak_dbfs }) => ({ name, source, dry_rms_dbfs, peak_dbfs })),
		variants,
		vetoes_applied: ['peak', 'finite', 'fast-sweep', 'detent-bit-exact', 'ring-down', 'crush-stereo-independent'],
		vetoes_not_applied: VETOES_NOT_APPLIED,
		pairs
	};
	assertPairsManifestShape(manifest);

	mkdirSync(join(outDir, 'clips'), { recursive: true });
	for (const [rel, channels] of clips) writeFileSync(join(outDir, rel), _encodeWav(channels, sr));
	writeFileSync(join(outDir, 'pairs.json'), `${JSON.stringify(manifest, null, 2)}\n`);
	return manifest;
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
		else if (flag === '--seconds') opts.seconds = Number(val);
		else if (flag === '--sample-rate') opts.sampleRate = Number(val);
		else if (flag === '--fixture') {
			const [name, path] = val.split('=');
			if (!name || !path) throw new Error(`--fixture expects <name>=<path.wav>, got ${val}`);
			opts.fixtures[name] = path;
		} else throw new Error(`unknown argument ${flag}`);
	}
	for (const k of ['seed', 'seconds', 'sampleRate']) if (k in opts && !(opts[k] > 0 || (k === 'seed' && opts[k] === 0))) throw new Error(`invalid --${k}`);
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
		const m = await exportSoundColorPairs({ ...opts, renderer });
		console.log(`[OK] wrote ${m.pairs.length} pairs, ${m.pairs.length * 2} clips, manifest ${join(opts.outDir, 'pairs.json')}, seed ${m.seed}`);
		return 0;
	} catch (err) {
		console.error(`[ERROR] ${err.message}`);
		return 1;
	}
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
	process.exitCode = await main(process.argv.slice(2));
}
