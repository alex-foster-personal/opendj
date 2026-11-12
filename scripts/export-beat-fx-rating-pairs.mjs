#!/usr/bin/env node
/**
 * Export blind A/B pairs for beat FX calibration: level law, default depth and OFF trails (#9789).
 *
 * Spec: (private path) section 6.
 *
 * Calibration clips render ONLY through a real-path renderer: the #9774 fx render harness driving
 * the #9782 beat FX unit graph and echo kernel. Until those land and an adapter is wired into
 * resolveRealRenderer, the CLI prints [UNAVAILABLE], exits 2 and writes nothing.
 *
 * Echo at 3/4 beat on channel 1. Sides are NOT loudness-matched; the level-law and depth questions
 * are the level difference. Proxies and seeded side order live in answer-key.json; pairs.json stays
 * blind for the #9776 page.
 *
 * Proxies (per side, answer-key.json): loudness_change_lu (K-weighted LU over steady window vs level-0
 * reference), wet_dry_db, tail_ratio_db (wet only), click_ratio (recorded, veto at 1.5 on engage/OFF).
 * click_ratio = max over t in {onSample, offSample} of secondDiffPeak(out)/max(secondDiffPeak(input), 1e-3)
 * with a 15 ms window.
 *
 * Usage:
 *   node scripts/export-beat-fx-rating-pairs.mjs --out <dir> [--seed <uint32>]
 *       [--sample-rate <hz>] [--fixture <name>=<path.wav>]
 *
 * Mini-PRD:
 *   R0 UNAVAILABLE without #9774 and #9782; no writes on exit 2
 *   R1 12 pairs, blind pairs.json, answer-key.json, seeded balanced sides, idempotent
 *   R2 proxies per side in answer key
 *   R3 finite, peak and click vetoes before write
 *   (R1-R3 exercised under a test double only.)
 *
 * requirement: BEATFX-10755
 */
import { mkdirSync, readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

// Mirror beat-fx.ts literals; this .mjs cannot import the .ts module.
const BEAT_FX_BEATS = 0.75;
const BEAT_FX_TARGET = '1';
const BEAT_FX_EFFECT = 'echo';

/**
 * @typedef {{ law: string, level: number, off: string, cutMs?: number }} FxParams
 * @typedef {{ question: string, variant: string, params: FxParams, effect: string, beats: number,
 *   target: string, input: Float64Array[], sampleRate: number, bpm: number, onSample: number,
 *   offSample: number }} RenderRequest
 * @typedef {{ out: Float64Array[], wet: Float64Array[] }} RenderResult
 * @typedef {{ id: string, realPath: boolean, render(req: RenderRequest): Promise<RenderResult> }} Renderer
 */

//----------------------------------------------------------------------------- CFG
export const DEFAULT_SEED = 0x5eed9789;
export const SAMPLE_RATE = 48000;
export const BPM = 124;
export const CLIP_BEATS = 40;
export const ON_BEAT = 4;
export const STEADY_FROM_BEAT = 12;
export const OFF_BEAT = 28;
export const TAIL_BEATS = 4;
export const TRAILS_LEVEL = 0.5;
export const FIXTURE_PEAK_DBFS = -1;
export const CLICK_RATIO_MAX = 1.5;
export const CLICK_WINDOW_MS = 15;
export const CLICK_REF_FLOOR = 1e-3;
export const TAIL_FLOOR_DB = -120;
export const EXIT_UNAVAILABLE = 2;
export const FIXTURE_NAMES = Object.freeze([
	'kick-bass',
	'chord-stab',
	'hats-perc',
	'house-124'
]);
export const QUESTIONS = Object.freeze({
	'level-law': {
		text: 'Level at full: which one sounds right?',
		variants: {
			'dry-held': { law: 'dry-held', level: 1, off: 'ring-out' },
			balance: { law: 'balance', level: 1, off: 'ring-out' }
		}
	},
	'default-depth': {
		text: 'Which echo depth sounds right as the default?',
		variants: {
			'depth-0.35': { law: 'balance', level: 0.35, off: 'ring-out' },
			'depth-0.5': { law: 'balance', level: 0.5, off: 'ring-out' }
		}
	},
	trails: {
		text: 'After the FX is switched off, which ending sounds right?',
		variants: {
			'ring-out': { law: 'balance', level: TRAILS_LEVEL, off: 'ring-out' },
			'cut-12ms': { law: 'balance', level: TRAILS_LEVEL, off: 'cut', cutMs: 12 }
		}
	}
});
export const REAL_PATH_DEPENDENCIES = Object.freeze([
	{
		issue: '#9774',
		what: 'fx render harness',
		path: 'apps/webui/frontend/tests/e2e/support/fx-render-harness.ts'
	},
	{
		issue: '#9782',
		what: 'beat FX unit graph and echo kernel',
		path: 'apps/webui/frontend/src/lib/rb/beat-fx-unit.ts'
	}
]);
const REPO_ROOT = fileURLToPath(new URL('../', import.meta.url));
const GENERATED_BY = 'scripts/export-beat-fx-rating-pairs.mjs';
const FLT_MIN_NORMAL = 1.1754943508222875e-38;
const VETOES_APPLIED = ['finite', 'peak', 'click'];
const VETOES_NOT_APPLIED = [
	{ veto: 'added-latency', reason: 'measured by the #9774 harness in #9782\'s own PR' },
	{
		veto: 'detent-bit-exact',
		reason: 'level 0, unit off, flag off; measured by the #9774 harness in #9782\'s own PR'
	},
	{ veto: 'no-runaway', reason: 'tail below -90 dB within 12 s; measured in #9782\'s own PR' },
	{
		veto: 'quantized-engage-2ms',
		reason: 'measured by the #9774 harness in #9782\'s own PR'
	},
	{
		veto: 'click-other-transitions',
		reason:
			'target switch, division change, Release FX are not rendered by this export; measured in #9786'
	}
];

export class VetoError extends Error {
	/** @param {{veto:string,file:string,question:string,fixture:string,variant:string,condition:string,measured:number,limit:number|string}} f */
	constructor(f) {
		super(
			`veto ${f.veto} failed: file=${f.file} question=${f.question} fixture=${f.fixture} variant=${f.variant} ${f.condition} measured=${f.measured} limit=${f.limit}`
		);
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
export function resolveRealRenderer(repoRoot = REPO_ROOT) {
	const missing = REAL_PATH_DEPENDENCIES.filter((d) => !existsSync(join(repoRoot, d.path)));
	if (missing.length > 0) {
		const names = missing.map((d) => `${d.issue} ${d.what} (${d.path})`).join(', ');
		throw new UnavailableError(
			`UNAVAILABLE: beat FX calibration renders only through the real unit; missing ${names}`,
			missing
		);
	}
	throw new UnavailableError(
		'UNAVAILABLE: the #9774 harness and #9782 beat FX unit are present but this script has no adapter to them yet; wire resolveRealRenderer',
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
const _round = (x, dp) => Math.round(x * 10 ** dp) / 10 ** dp + 0;
function _rms(channels, from = 0, to = channels[0].length) {
	let sum = 0;
	const len = to - from;
	if (len <= 0) return 0;
	for (const ch of channels) for (let i = from; i < to; i++) sum += ch[i] * ch[i];
	return Math.sqrt(sum / (channels.length * len));
}
function _shuffle(items, next) {
	for (let i = items.length - 1; i > 0; i--) {
		const j = Math.floor(next() * (i + 1));
		[items[i], items[j]] = [items[j], items[i]];
	}
}
const _id = (prefix, idx) => `${prefix}${String(idx + 1).padStart(2, '0')}`;
const _beatToSample = (beats, bpm, sr) => Math.round(((beats * 60) / bpm) * sr);

/** K-weighted loudness (LU) over [from, to); port of 5d1ef01f7e shortTermLufs with a fixed window. */
function _kWeightedLoudnessLu(channels, from, to, sr) {
	const hp = 0.996;
	let mean = 0;
	for (const ch of channels) {
		let z1 = 0;
		let z2 = 0;
		for (let i = 0; i < to; i++) {
			const x = ch[i];
			z1 = hp * (z1 + x - (i > 0 ? ch[i - 1] : 0));
			const y = z1 - z2;
			z2 = hp * (z2 + y);
			const k = 1.53 * y - 0.45 * z1;
			if (i >= from) mean += k * k;
		}
	}
	const winLen = to - from;
	if (winLen <= 0) throw new Error('loudness window is empty');
	mean /= channels.length * winLen;
	if (mean <= 0) return -Infinity;
	return -0.691 + 10 * Math.log10(mean);
}

function _secondDiffPeak(channels, from, to) {
	let m = 0;
	for (const ch of channels) {
		for (let i = Math.max(2, from); i < Math.min(ch.length, to); i++)
			m = Math.max(m, Math.abs(ch[i] - 2 * ch[i - 1] + ch[i - 2]));
	}
	return m;
}

function _clickRatio(out, input, sample, sr) {
	const w = Math.round((CLICK_WINDOW_MS * sr) / 1000);
	const outPeak = _secondDiffPeak(out, sample - w, sample + w);
	const inPeak = Math.max(_secondDiffPeak(input, sample - w, sample + w), CLICK_REF_FLOOR);
	return outPeak / inPeak;
}

//----------------------------------------------------------------------------- fixtures
function _house124(n, sr, bpm, seed) {
	const beat = (60 / bpm) * sr;
	const chord = [130.81, 164.81, 196];
	const out = [new Float64Array(n), new Float64Array(n)];
	out.forEach((buf, c) => {
		const next = _rng(seed ^ (0x1000 + c));
		let bassPh = 0;
		const chordPh = chord.map(() => 0);
		for (let i = 0; i < n; i++) {
			const inBeat = (i % beat) / sr;
			const pos = (i / beat) % 1;
			const kickPh = 2 * Math.PI * (50 * inBeat + 4 * (1 - Math.exp(-inBeat / 0.04)));
			let x = Math.sin(kickPh) * Math.exp(-inBeat / 0.25);
			if (pos >= 0.5 && pos < 0.5 + 0.03 / (60 / bpm))
				x += 0.15 * (next() * 2 - 1) * Math.exp(-(pos - 0.5) * 20);
			bassPh += (2 * Math.PI * 55) / sr;
			if (pos >= 0.5) x += 0.4 * Math.sin(bassPh);
			let saw = 0;
			chord.forEach((f, k) => {
				chordPh[k] += (2 * Math.PI * f * (1 + 0.002 * c)) / sr;
				for (let h = 1; h <= 6; h++) saw += Math.sin(h * chordPh[k]) / h;
			});
			buf[i] = x + 0.12 * saw;
		}
	});
	return out;
}
function _kickBass(n, sr, bpm, seed) {
	const beat = (60 / bpm) * sr;
	const out = [new Float64Array(n), new Float64Array(n)];
	out.forEach((buf, c) => {
		const next = _rng(seed ^ (0x2000 + c));
		let bassPh = 0;
		for (let i = 0; i < n; i++) {
			const pos = (i / beat) % 1;
			const inBeat = (i % beat) / sr;
			const kickPh = 2 * Math.PI * (60 * inBeat);
			let x = Math.sin(kickPh) * Math.exp(-inBeat / 0.2) * (pos < 0.05 ? 1 : 0);
			bassPh += (2 * Math.PI * 55) / sr;
			if (pos >= 0.5 && pos < 0.55) x += 0.55 * Math.sin(bassPh);
			buf[i] = x + 0.02 * (next() - 0.5);
		}
	});
	return out;
}
function _chordStab(n, sr, bpm, seed) {
	const beat = (60 / bpm) * sr;
	const chord = [130.81, 196, 246.94];
	const out = [new Float64Array(n), new Float64Array(n)];
	out.forEach((buf, c) => {
		const chordPh = chord.map(() => 0);
		let env = 0;
		for (let i = 0; i < n; i++) {
			const pos = (i / beat) % 1;
			const target = pos >= 0.5 && pos < 0.65 ? 1 : 0;
			env += (target - env) * 0.002;
			let x = 0;
			chord.forEach((f, k) => {
				chordPh[k] += (2 * Math.PI * f * (1 + 0.001 * c)) / sr;
				for (let h = 1; h <= 4; h++) x += Math.sin(h * chordPh[k]) / h;
			});
			buf[i] = x * 0.28 * env;
		}
	});
	return out;
}
function _hatsPerc(n, sr, bpm, seed) {
	const beat = (60 / bpm) * sr;
	const sixteenth = beat / 4;
	const out = [new Float64Array(n), new Float64Array(n)];
	out.forEach((buf, c) => {
		const next = _rng(seed ^ (0x3000 + c));
		for (let i = 0; i < n; i++) {
			const pos16 = (i % sixteenth) / sixteenth;
			const barPos = (i / beat) % 1;
			let x = 0;
			if (pos16 < 0.15) x += 0.25 * (next() * 2 - 1) * (1 - pos16 / 0.15);
			if (barPos >= 0.5 && barPos < 0.53) x += 0.4 * (next() * 2 - 1) * Math.exp(-(barPos - 0.5) * 40);
			buf[i] = x;
		}
	});
	return out;
}
const _FIXTURE_SYNTH = {
	'kick-bass': _kickBass,
	'chord-stab': _chordStab,
	'hats-perc': _hatsPerc,
	'house-124': _house124
};
function _normalisePeak(name, source, channels, peakDbfs) {
	let peak = 0;
	for (const ch of channels) for (const x of ch) peak = Math.max(peak, Math.abs(x));
	const targetLin = _dbToLin(peakDbfs);
	const scale = peak > 0 ? targetLin / peak : 1;
	const scaled = channels.map((ch) => Float64Array.from(ch, (x) => x * scale));
	return {
		name,
		source,
		channels: scaled,
		peak_dbfs: _round(peakDbfs, 2),
		rms_dbfs: _round(20 * Math.log10(_rms(scaled)), 2)
	};
}
function _buildFixtures(n, sr, bpm, seed, overrides) {
	return FIXTURE_NAMES.map((name) => {
		const path = overrides?.[name];
		const raw = path
			? _readWav(path, sr, n)
			: _FIXTURE_SYNTH[name](n, sr, bpm, seed ^ name.split('').reduce((a, c) => a + c.charCodeAt(0), 0));
		return _normalisePeak(name, path ? 'wav' : 'synthesized', raw, FIXTURE_PEAK_DBFS);
	});
}

//----------------------------------------------------------------------------- vetoes and contract
function _checkFinite(channels, ctx) {
	for (const ch of channels) {
		for (const x of ch) {
			const f = Math.fround(x);
			if (!Number.isFinite(f) || (f !== 0 && Math.abs(f) < FLT_MIN_NORMAL))
				throw new VetoError({
					veto: 'finite',
					...ctx,
					measured: f,
					limit: 'finite normal float32'
				});
		}
	}
}
function _checkPeak(channels, ctx) {
	for (const ch of channels)
		for (const x of ch)
			if (Math.abs(x) > 1)
				throw new VetoError({ veto: 'peak', ...ctx, measured: Math.abs(x), limit: 1 });
}
function _checkClick(out, input, onSample, offSample, sr, ctx) {
	for (const sample of [onSample, offSample]) {
		const ratio = _clickRatio(out, input, sample, sr);
		if (ratio > CLICK_RATIO_MAX)
			throw new VetoError({
				veto: 'click',
				...ctx,
				condition: `click at sample ${sample}`,
				measured: _round(ratio, 3),
				limit: CLICK_RATIO_MAX
			});
	}
}
function _checkContract(renderer, result, n) {
	const broke = (why) => {
		throw new Error(`renderer ${renderer.id} broke the contract: ${why}`);
	};
	if (
		!Array.isArray(result?.out) ||
		result.out.length !== 2 ||
		!result.out.every((ch) => ch instanceof Float64Array && ch.length === n)
	)
		broke(`out must be two Float64Array of length ${n}`);
	if (
		!Array.isArray(result?.wet) ||
		result.wet.length !== 2 ||
		!result.wet.every((ch) => ch instanceof Float64Array && ch.length === n)
	)
		broke(`wet must be two Float64Array of length ${n}`);
}

function _refKey(fixture, params) {
	return `${fixture}|${params.law}|${params.off}|${params.cutMs ?? 0}`;
}

export function assertFiniteBeatFxProxies(proxies, fixture) {
	for (const key of ['loudness_change_lu', 'wet_dry_db', 'tail_ratio_db']) {
		if (!Number.isFinite(proxies[key]))
			throw new Error(`non-finite proxy ${key} (${fixture})`);
	}
}

function _proxies(render, wet, input, refOut, sr, bpm, offSample, fixture) {
	const s0 = _beatToSample(STEADY_FROM_BEAT, bpm, sr);
	const s1 = _beatToSample(STEADY_FROM_BEAT + 16, bpm, sr);
	const t0 = offSample;
	const t1 = _beatToSample(OFF_BEAT + TAIL_BEATS, bpm, sr);
	const p0 = _beatToSample(OFF_BEAT - 4, bpm, sr);
	const wetPre = _rms(wet, p0, t0);
	if (wetPre === 0) throw new Error('wet pre-OFF rms is zero; tail_ratio_db is undefined');
	const tailLin = _rms(wet, t0, t1) / wetPre;
	const tailDb = 20 * Math.log10(tailLin);
	const proxies = {
		loudness_change_lu: _round(
			_kWeightedLoudnessLu(render, s0, s1, sr) - _kWeightedLoudnessLu(refOut, s0, s1, sr),
			2
		),
		wet_dry_db: _round(20 * Math.log10(_rms(wet, s0, s1) / _rms(input, s0, s1)), 2),
		tail_ratio_db: _round(Math.max(tailDb, TAIL_FLOOR_DB), 2)
	};
	assertFiniteBeatFxProxies(proxies, fixture);
	return proxies;
}

//----------------------------------------------------------------------------- manifest shape
/** @param {any} manifest */
export function assertBeatFxPairsManifestShape(manifest) {
	const fail = (field) => {
		throw new Error(`beat-fx pairs manifest shape: ${field}`);
	};
	if (manifest?.family !== 'beat-fx') fail('family must be beat-fx');
	if (manifest?.round !== 1) fail('round must be 1');
	if (typeof manifest.renderer?.id !== 'string' || typeof manifest.renderer?.real_path !== 'boolean')
		fail('renderer');
	if (!Array.isArray(manifest.pairs) || manifest.pairs.length !== 12) fail('pairs length 12');
	const qKeys = Object.keys(QUESTIONS);
	const texts = new Set(qKeys.map((k) => QUESTIONS[k].text));
	const fixtures = new Set(FIXTURE_NAMES);
	const ids = new Set();
	for (const p of manifest.pairs) {
		if (!/^p\d{2}$/.test(p.id) || ids.has(p.id)) fail(`pair id ${p.id}`);
		ids.add(p.id);
		if (typeof p.a !== 'string' || typeof p.b !== 'string' || p.a === p.b) fail(`${p.id} a/b`);
		if (!/^clips\/p\d{2}-[ab]\.wav$/.test(p.a) || !/^clips\/p\d{2}-[ab]\.wav$/.test(p.b))
			fail(`${p.id} clip paths`);
		if (!texts.has(p.question)) fail(`${p.id} question text`);
		if (!qKeys.includes(p.meta?.question_key)) fail(`${p.id} question_key`);
		if (!fixtures.has(p.meta?.fixture)) fail(`${p.id} fixture`);
	}
	const variantIds = Object.values(QUESTIONS).flatMap((q) => Object.keys(q.variants));
	const blob = JSON.stringify(manifest);
	for (const vid of variantIds) {
		if (blob.includes(vid)) fail(`variant id ${vid} leaked into manifest`);
	}
	if (blob.includes('seed') || blob.includes('loudness_change')) fail('non-blind field in manifest');
}

//----------------------------------------------------------------------------- WAV IO
function _encodeWav(channels, sr) {
	const n = channels[0].length;
	const buf = Buffer.alloc(44 + n * 4);
	buf.write('RIFF', 0);
	buf.writeUInt32LE(36 + n * 4, 4);
	buf.write('WAVE', 8);
	buf.write('fmt ', 12);
	buf.writeUInt32LE(16, 16);
	buf.writeUInt16LE(1, 20);
	buf.writeUInt16LE(2, 22);
	buf.writeUInt32LE(sr, 24);
	buf.writeUInt32LE(sr * 4, 28);
	buf.writeUInt16LE(4, 32);
	buf.writeUInt16LE(16, 34);
	buf.write('data', 36);
	buf.writeUInt32LE(n * 4, 40);
	for (let i = 0; i < n; i++) {
		for (let c = 0; c < 2; c++)
			buf.writeInt16LE(
				Math.round(Math.max(-1, Math.min(1, channels[c][i])) * 32767),
				44 + i * 4 + c * 2
			);
	}
	return buf;
}
function _readWav(path, sr, n) {
	const buf = readFileSync(path);
	if (buf.toString('ascii', 0, 4) !== 'RIFF' || buf.toString('ascii', 8, 12) !== 'WAVE')
		throw new Error(`${path} is not RIFF/WAVE`);
	let off = 12;
	let fmt = null;
	while (off + 8 <= buf.length) {
		const id = buf.toString('ascii', off, off + 4);
		const size = buf.readUInt32LE(off + 4);
		if (id === 'fmt ')
			fmt = {
				format: buf.readUInt16LE(off + 8),
				ch: buf.readUInt16LE(off + 10),
				rate: buf.readUInt32LE(off + 12),
				bits: buf.readUInt16LE(off + 22)
			};
		else if (id === 'data') {
			if (!fmt) throw new Error(`${path} has data before fmt`);
			if (fmt.format !== 1 || fmt.bits !== 16 || fmt.ch !== 2)
				throw new Error(`${path} must be 16-bit stereo PCM`);
			if (fmt.rate !== sr) throw new Error(`${path} sample rate ${fmt.rate} != ${sr}`);
			const frames = Math.floor(size / 4);
			if (frames < n) throw new Error(`${path} has ${frames} frames, needs ${n}`);
			return [0, 1].map((c) =>
				Float64Array.from({ length: n }, (_, i) => buf.readInt16LE(off + 8 + i * 4 + c * 2) / 32768)
			);
		}
		off += 8 + size + (size % 2);
	}
	throw new Error(`${path} has no data chunk`);
}

//----------------------------------------------------------------------------- export
/**
 * @param {{outDir:string, renderer:Renderer, allowTestDouble?:boolean, seed?:number, sampleRate?:number, bpm?:number, fixtures?:Record<string,string>}} opts
 */
export async function exportBeatFxRatingPairs({
	outDir,
	renderer,
	allowTestDouble = false,
	seed = DEFAULT_SEED,
	sampleRate = SAMPLE_RATE,
	bpm = BPM,
	fixtures
}) {
	if (
		typeof renderer?.id !== 'string' ||
		typeof renderer.realPath !== 'boolean' ||
		typeof renderer.render !== 'function'
	)
		throw new Error('renderer {id, realPath, render} is required');
	if (renderer.realPath !== true && !allowTestDouble)
		throw new Error(
			`renderer ${renderer.id} is not a real-path renderer; beat FX calibration needs the #9774 harness (pass allowTestDouble only from tests)`
		);
	if (!outDir) throw new Error('--out <dir> is required');
	if (existsSync(outDir) && readdirSync(outDir).length > 0)
		throw new Error(`--out ${outDir} is not empty; refusing to overwrite an earlier rating round`);
	seed >>>= 0;
	const sr = sampleRate;
	const n = _beatToSample(CLIP_BEATS, bpm, sr);
	const onSample = _beatToSample(ON_BEAT, bpm, sr);
	const offSample = _beatToSample(OFF_BEAT, bpm, sr);
	const fixtureList = _buildFixtures(n, sr, bpm, seed, fixtures);
	const fixtureByName = Object.fromEntries(fixtureList.map((f) => [f.name, f]));

	const combos = [];
	for (const qKey of Object.keys(QUESTIONS))
		for (const fixture of FIXTURE_NAMES) combos.push({ question_key: qKey, fixture });

	const next = _rng(seed);
	_shuffle(combos, next);
	combos.forEach((c, idx) => {
		c.id = _id('p', idx);
	});

	const sideOrder = {};
	for (const qKey of Object.keys(QUESTIONS)) {
		const slots = [true, true, false, false];
		_shuffle(slots, next);
		sideOrder[qKey] = slots;
	}

	const slotIdx = Object.fromEntries(Object.keys(QUESTIONS).map((k) => [k, 0]));
	for (const c of combos) {
		const firstOnA = sideOrder[c.question_key][slotIdx[c.question_key]++];
		const variantKeys = Object.keys(QUESTIONS[c.question_key].variants);
		c.aVariant = firstOnA ? variantKeys[0] : variantKeys[1];
		c.bVariant = firstOnA ? variantKeys[1] : variantKeys[0];
		c.aFile = `clips/${c.id}-a.wav`;
		c.bFile = `clips/${c.id}-b.wav`;
	}

	const renders = new Map();
	const refRenders = new Map();
	const renderOne = async (fixture, qKey, variant, file, levelOverride) => {
		const params = { ...QUESTIONS[qKey].variants[variant] };
		if (levelOverride !== undefined) params.level = levelOverride;
		const key = `${fixture}|${qKey}|${variant}|${levelOverride ?? 'full'}`;
		if (renders.has(key)) return renders.get(key);
		const input = fixtureByName[fixture].channels;
		const req = {
			question: QUESTIONS[qKey].text,
			variant,
			params,
			effect: BEAT_FX_EFFECT,
			beats: BEAT_FX_BEATS,
			target: BEAT_FX_TARGET,
			input,
			sampleRate: sr,
			bpm,
			onSample,
			offSample
		};
		const result = await renderer.render(req);
		_checkContract(renderer, result, n);
		const ctx = {
			file,
			question: qKey,
			fixture,
			variant,
			condition: `level=${params.level}`
		};
		_checkFinite(result.out, ctx);
		_checkPeak(result.out, ctx);
		_checkClick(result.out, input, onSample, offSample, sr, ctx);
		const pack = { ...result, input, params };
		renders.set(key, pack);
		return pack;
	};

	for (const c of combos) {
		await renderOne(c.fixture, c.question_key, c.aVariant, c.aFile);
		await renderOne(c.fixture, c.question_key, c.bVariant, c.bFile);
	}

	for (const f of FIXTURE_NAMES) {
		for (const qKey of Object.keys(QUESTIONS)) {
			for (const variant of Object.keys(QUESTIONS[qKey].variants)) {
				const params = QUESTIONS[qKey].variants[variant];
				const rk = _refKey(f, params);
				if (refRenders.has(rk)) continue;
				const input = fixtureByName[f].channels;
				const req = {
					question: QUESTIONS[qKey].text,
					variant,
					params: { ...params, level: 0 },
					effect: BEAT_FX_EFFECT,
					beats: BEAT_FX_BEATS,
					target: BEAT_FX_TARGET,
					input,
					sampleRate: sr,
					bpm,
					onSample,
					offSample
				};
				const result = await renderer.render(req);
				_checkContract(renderer, result, n);
				_checkFinite(result.out, {
					file: '(level-0 reference)',
					question: qKey,
					fixture: f,
					variant,
					condition: 'level=0 reference'
				});
				refRenders.set(rk, result);
			}
		}
	}

	const answerPairs = {};
	const clips = [];
	for (const c of combos) {
		const side = (variant, file) => {
			const r = renders.get(`${c.fixture}|${c.question_key}|${variant}|full`);
			const ref = refRenders.get(_refKey(c.fixture, r.params));
			const prox = _proxies(r.out, r.wet, r.input, ref.out, sr, bpm, offSample, c.fixture);
			const clickEng = _round(_clickRatio(r.out, r.input, onSample, sr), 3);
			const clickOff = _round(_clickRatio(r.out, r.input, offSample, sr), 3);
			clips.push([file, r.out]);
			return {
				variant,
				...prox,
				click_ratio: Math.max(clickEng, clickOff)
			};
		};
		answerPairs[c.id] = {
			question_key: c.question_key,
			fixture: c.fixture,
			a: side(c.aVariant, c.aFile),
			b: side(c.bVariant, c.bFile)
		};
	}

	const manifestPairs = combos.map((c) => ({
		id: c.id,
		a: c.aFile,
		b: c.bFile,
		question: QUESTIONS[c.question_key].text,
		meta: { question_key: c.question_key, fixture: c.fixture }
	}));

	const questions = {};
	for (const k of Object.keys(QUESTIONS)) questions[k] = QUESTIONS[k].text;

	const manifest = {
		family: 'beat-fx',
		round: 1,
		generated_by: GENERATED_BY,
		renderer: { id: renderer.id, real_path: renderer.realPath },
		sample_rate: sr,
		bpm,
		effect: BEAT_FX_EFFECT,
		beats: BEAT_FX_BEATS,
		target: BEAT_FX_TARGET,
		clip: {
			beats: CLIP_BEATS,
			on_beat: ON_BEAT,
			off_beat: OFF_BEAT,
			steady_window_beats: [STEADY_FROM_BEAT, STEADY_FROM_BEAT + 16],
			tail_beats: TAIL_BEATS
		},
		fixtures: fixtureList.map(({ name, source, peak_dbfs, rms_dbfs }) => ({
			name,
			source,
			peak_dbfs,
			rms_dbfs
		})),
		questions,
		vetoes_applied: VETOES_APPLIED,
		vetoes_not_applied: VETOES_NOT_APPLIED,
		pairs: manifestPairs
	};

	const answerKey = {
		family: 'beat-fx',
		round: 1,
		seed,
		variants: QUESTIONS,
		pairs: answerPairs
	};

	assertBeatFxPairsManifestShape(manifest);

	mkdirSync(join(outDir, 'clips'), { recursive: true });
	for (const [rel, channels] of clips) writeFileSync(join(outDir, rel), _encodeWav(channels, sr));
	writeFileSync(join(outDir, 'pairs.json'), `${JSON.stringify(manifest, null, 2)}\n`);
	writeFileSync(join(outDir, 'answer-key.json'), `${JSON.stringify(answerKey, null, 2)}\n`);
	return { manifest, answerKey };
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
		else if (flag === '--sample-rate') opts.sampleRate = Number(val);
		else if (flag === '--fixture') {
			const eq = val.indexOf('=');
			const name = val.slice(0, eq);
			const path = val.slice(eq + 1);
			if (!name || !path) throw new Error(`--fixture expects <name>=<path.wav>, got ${val}`);
			if (!FIXTURE_NAMES.includes(name)) throw new Error(`unknown fixture ${name}`);
			opts.fixtures[name] = path;
		} else throw new Error(`unknown argument ${flag}`);
	}
	if ('seed' in opts && !(Number.isInteger(opts.seed) && opts.seed >= 0 && opts.seed <= 0xffffffff))
		throw new Error('invalid --seed (uint32)');
	if ('sampleRate' in opts && !(Number.isInteger(opts.sampleRate) && opts.sampleRate > 0))
		throw new Error('invalid --sample-rate');
	if (!opts.outDir) throw new Error('--out <dir> is required');
	return opts;
}

export async function main(argv, testOpts = {}) {
	let opts;
	try {
		opts = _parseArgs(argv);
	} catch (err) {
		console.error(`[ERROR] ${err.message}`);
		return 1;
	}
	let renderer = testOpts.renderer;
	if (!renderer) {
		try {
			renderer = resolveRealRenderer();
		} catch (err) {
			if (!(err instanceof UnavailableError)) throw err;
			console.error(`[UNAVAILABLE] ${err.message}`);
			return EXIT_UNAVAILABLE;
		}
	}
	try {
		await exportBeatFxRatingPairs({
			...opts,
			renderer,
			allowTestDouble: testOpts.allowTestDouble === true
		});
		console.log(
			`[OK] wrote 12 pairs, 24 clips, manifest ${join(opts.outDir, 'pairs.json')}, answer key ${join(opts.outDir, 'answer-key.json')}`
		);
		return 0;
	} catch (err) {
		console.error(`[ERROR] ${err.message}`);
		return 1;
	}
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
	process.exitCode = await main(process.argv.slice(2));
}
