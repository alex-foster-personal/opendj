#!/usr/bin/env node
/**
 * Export blind A/B pairs for PITCH, SHIMMER and STUTTER buffer-space FX variants (issue #10299).
 *
 * Calibration clips render ONLY through the #9774 fx render harness and the #10222, #10228 and
 * #10226 kernels. Until those land and resolveRealRenderer is wired, the CLI prints [UNAVAILABLE],
 * exits 2 and writes nothing. Pairing, blinding, short-term LUFS match, manifest, vetoes and WAV
 * writing run behind the Renderer interface below.
 *
 * Usage:
 *   node apps/webui/frontend/scripts/export-buffer-space-fx-pairs.mjs --out <dir>
 *       [--seed <uint32>] [--sample-rate <hz>] [--bars <n>]
 *       [--fixture <name>=<path.wav>]...
 */
import { mkdirSync, readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

/**
 * @typedef {{ kernel: 'PITCH'|'SHIMMER'|'STUTTER', axis: string, variant: string, constants: object,
 *   input: Float64Array[], sampleRate: number, bpm: number, seed: number, depth: number,
 *   pitchSemitones?: number }} RenderRequest
 * @typedef {{ id: string, realPath: boolean, render(req: RenderRequest): Promise<Float64Array[]> }} Renderer
 */

//----------------------------------------------------------------------------- CFG
export const DEFAULT_SEED = 0x5eed1029;
export const SAMPLE_RATE = 48000;
export const BPM = 124;
export const CLIP_BARS = 8;
export const BEATS_PER_BAR = 4;
export const DEPTH = 0.7;
export const LUFS_MATCH_MAX_DB = 0.5;
export const DRY_RMS_DBFS = -23;
export const FIXTURE_PEAK_MAX_DBFS = -3;
export const FIXTURE_NAMES = ['house-124', 'vocal', 'chord'];
export const KERNELS = ['PITCH', 'SHIMMER', 'STUTTER'];
export const QUESTION = 'Which FX sounds better?';
export const EXIT_UNAVAILABLE = 2;
export const PAIR_COUNTS = Object.freeze({ PITCH: 8, SHIMMER: 4, STUTTER: 4 });
export const REAL_PATH_DEPENDENCIES = Object.freeze([
	{
		issue: '#9774',
		what: 'fx render harness',
		path: 'apps/webui/frontend/tests/e2e/support/fx-render-harness.ts'
	},
	{
		issue: '#10222',
		what: 'PITCH beat FX kernel',
		path: 'apps/webui/frontend/src/lib/rb/pitch-fx-kernel.ts'
	},
	{
		issue: '#10228',
		what: 'SHIMMER beat FX kernel',
		path: 'apps/webui/frontend/src/lib/rb/shimmer-fx-kernel.ts'
	},
	{
		issue: '#10226',
		what: 'STUTTER beat FX kernel',
		path: 'apps/webui/frontend/src/lib/rb/stutter-fx-kernel.ts'
	}
]);
/** Registered variant constants keyed for the manifest sidecar (#10045). */
export const VARIANTS = Object.freeze({
	PITCH: {
		20: { setting: 'variant_fx_pitch_window', windowMs: 20 },
		40: { setting: 'variant_fx_pitch_window', windowMs: 40 },
		80: { setting: 'variant_fx_pitch_window', windowMs: 80 }
	},
	SHIMMER: {
		interval: {
			'+12': { setting: 'variant_fx_shimmer_interval', semitones: 12 },
			'+7': { setting: 'variant_fx_shimmer_interval', semitones: 7 }
		},
		sparkle: {
			0.5: { setting: 'variant_fx_shimmer_sparkle', sparkleRate: 0.5 },
			0.8: { setting: 'variant_fx_shimmer_sparkle', sparkleRate: 0.8 }
		}
	},
	STUTTER: {
		chunk: {
			'1/4': { setting: 'variant_fx_stutter_chunk', chunkBeats: 0.25 },
			'1/8': { setting: 'variant_fx_stutter_chunk', chunkBeats: 0.125 }
		},
		blend: {
			'equal-power': { setting: 'variant_fx_stutter_blend', blend: 'equal-power' },
			'dry-held': { setting: 'variant_fx_stutter_blend', blend: 'dry-held' }
		}
	}
});
const REPO_ROOT = fileURLToPath(new URL('../../../../', import.meta.url));
const GENERATED_BY = 'apps/webui/frontend/scripts/export-buffer-space-fx-pairs.mjs';
const FLT_MIN_NORMAL = 1.1754943508222875e-38;
const VETOES_NOT_APPLIED = [
	{ veto: 'added-latency', reason: "measured by the #9774 harness in each kernel's own PR" },
	{ veto: 'click-ratio', reason: "measured by the #9774 harness in each kernel's own PR" },
	{ veto: 'tail-decay', reason: "measured by the #9774 harness in each kernel's own PR" }
];

export class VetoError extends Error {
	/** @param {{veto:string,file:string,kernel:string,variant:string,measured:number,limit:number|string}} f */
	constructor(f) {
		super(
			`veto ${f.veto} failed: file=${f.file} kernel=${f.kernel} variant=${f.variant} measured=${f.measured} limit=${f.limit}`
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
			`UNAVAILABLE: buffer-space calibration renders only through the real kernels; missing ${names}`,
			missing
		);
	}
	throw new UnavailableError(
		'UNAVAILABLE: the #9774 harness and kernels are present but this script has no adapter to them yet; wire resolveRealRenderer',
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
	let n = 0;
	for (const ch of channels)
		for (let i = from; i < to; i++) {
			sum += ch[i] * ch[i];
			n++;
		}
	return Math.sqrt(sum / n);
}
function _shuffle(items, next) {
	for (let i = items.length - 1; i > 0; i--) {
		const j = Math.floor(next() * (i + 1));
		[items[i], items[j]] = [items[j], items[i]];
	}
}
const _beatToSample = (beats, bpm, sr) => Math.round(((beats * 60) / bpm) * sr);

/** ITU-R BS.1770 short-term LUFS (3 s gated window, K-weighted stereo sum). */
export function shortTermLufs(channels, sr) {
	const len = channels[0].length;
	const win = Math.min(len, Math.max(1, Math.round(3 * sr)));
	const step = Math.max(1, Math.round(0.1 * sr));
	const hp = 0.996;
	let maxMean = 0;
	const starts = len <= win ? [0] : [];
	for (let start = 0; start + win <= len; start += step) starts.push(start);
	for (const start of starts) {
		let mean = 0;
		for (const ch of channels) {
			let z1 = 0;
			let z2 = 0;
			for (let i = start; i < start + win; i++) {
				const x = ch[i];
				z1 = hp * (z1 + x - (i > start ? ch[i - 1] : 0));
				const y = z1 - z2;
				z2 = hp * (z2 + y);
				const k = 1.53 * y - 0.45 * z1;
				mean += k * k;
			}
		}
		mean /= 2 * win;
		maxMean = Math.max(maxMean, mean);
	}
	if (maxMean <= 0) return -Infinity;
	return -0.691 + 10 * Math.log10(maxMean);
}

/** Level-match two renders so short-term LUFS differ by at most LUFS_MATCH_MAX_DB. */
export function levelMatchPair(aIn, bIn, sr) {
	let lufsA = shortTermLufs(aIn, sr);
	let lufsB = shortTermLufs(bIn, sr);
	if (!Number.isFinite(lufsA) || !Number.isFinite(lufsB)) {
		throw new Error('short-term LUFS is non-finite on a pair side');
	}
	const mid = (lufsA + lufsB) / 2;
	const gainDbA = _round(mid - lufsA, 2);
	const gainDbB = _round(mid - lufsB, 2);
	const ga = _dbToLin(gainDbA);
	const gb = _dbToLin(gainDbB);
	const aOut = aIn.map((ch) => Float64Array.from(ch, (x) => x * ga));
	const bOut = bIn.map((ch) => Float64Array.from(ch, (x) => x * gb));
	lufsA = shortTermLufs(aOut, sr);
	lufsB = shortTermLufs(bOut, sr);
	return {
		a: aOut,
		b: bOut,
		lufs_a: _round(lufsA, 2),
		lufs_b: _round(lufsB, 2),
		match_gain_db: { a: gainDbA, b: gainDbB },
		lufs_delta_db: _round(Math.abs(lufsA - lufsB), 3)
	};
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
function _vocalPhrase(n, sr, seed) {
	const beat = ((60 / BPM) * sr) | 0;
	const next = _rng(seed ^ 0xabcd);
	const out = [new Float64Array(n), new Float64Array(n)];
	const notes = [220, 246.94, 261.63, 293.66];
	let ph = 0;
	for (let c = 0; c < 2; c++) {
		ph = 0;
		for (let i = 0; i < n; i++) {
			const f0 = notes[Math.floor(i / (2 * beat)) % notes.length];
			ph += (2 * Math.PI * f0) / sr;
			let saw = 0;
			for (let h = 1; h <= 8; h++) saw += Math.sin(h * ph + c * 0.1) / h;
			const env = Math.sin(Math.PI * Math.min(1, (i % (4 * beat)) / (4 * beat))) ** 2;
			out[c][i] = 0.35 * saw * env + 0.02 * (next() * 2 - 1) * env;
		}
	}
	return out;
}
function _sustainedChord(n, sr, seed) {
	const freqs = [130.81, 164.81, 196, 246.94];
	const out = [new Float64Array(n), new Float64Array(n)];
	const ph = freqs.map(() => 0);
	for (let c = 0; c < 2; c++) {
		for (let i = 0; i < n; i++) {
			let sum = 0;
			freqs.forEach((f, k) => {
				ph[k] += (2 * Math.PI * f * (1 + 0.001 * c)) / sr;
				sum += Math.sin(ph[k] + seed * 1e-9);
			});
			out[c][i] = (0.22 * sum) / freqs.length;
		}
	}
	return out;
}
function _normaliseFixture(name, source, channels) {
	const gain = _dbToLin(DRY_RMS_DBFS) / _rms(channels);
	const scaled = channels.map((ch) => Float64Array.from(ch, (x) => x * gain));
	let peak = 0;
	for (const ch of scaled) for (const x of ch) peak = Math.max(peak, Math.abs(x));
	const peakDb = _round(20 * Math.log10(peak), 2);
	if (peakDb > FIXTURE_PEAK_MAX_DBFS) {
		throw new Error(
			`fixture ${name} peaks at ${peakDb} dBFS after RMS normalisation to ${DRY_RMS_DBFS} dBFS, limit ${FIXTURE_PEAK_MAX_DBFS}`
		);
	}
	return { name, source, channels: scaled, dry_rms_dbfs: DRY_RMS_DBFS, peak_dbfs: peakDb };
}
function _buildFixtures(n, sr, bpm, seed, overrides) {
	const synth = {
		'house-124': () => _house124(n, sr, bpm, seed),
		vocal: () => _vocalPhrase(n, sr, seed ^ 0x10299),
		chord: () => _sustainedChord(n, sr, seed ^ 0x20499)
	};
	return FIXTURE_NAMES.map((name) => {
		const path = overrides?.[name];
		if (path) return _normaliseFixture(name, 'wav', _readWav(path, sr, n));
		return _normaliseFixture(name, 'synthesized', synth[name]());
	});
}

//----------------------------------------------------------------------------- vetoes
function _checkFiniteAndPeak(channels, ctx) {
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
	for (const ch of channels)
		for (const x of ch)
			if (Math.abs(x) > 1)
				throw new VetoError({ veto: 'peak', ...ctx, measured: Math.abs(x), limit: 1 });
}

//----------------------------------------------------------------------------- canonical pairs
/** @returns {{kernel:string,axis:string,a:string,b:string,fixture:string,pitchSemitones?:number}[]} */
export function canonicalPairs() {
	const out = [];
	const pitchConditions = [
		{ fixture: 'vocal', pitchSemitones: 7 },
		{ fixture: 'vocal', pitchSemitones: -5 },
		{ fixture: 'chord', pitchSemitones: 7 },
		{ fixture: 'chord', pitchSemitones: -5 }
	];
	const windowPairs = [
		['20', '40'],
		['40', '80']
	];
	for (const cond of pitchConditions) {
		for (const [a, b] of windowPairs) {
			out.push({
				kernel: 'PITCH',
				axis: 'window',
				a,
				b,
				fixture: cond.fixture,
				pitchSemitones: cond.pitchSemitones
			});
		}
	}
	out.push(
		{ kernel: 'SHIMMER', axis: 'interval', a: '+12', b: '+7', fixture: 'house-124' },
		{ kernel: 'SHIMMER', axis: 'interval', a: '+12', b: '+7', fixture: 'vocal' },
		{ kernel: 'SHIMMER', axis: 'sparkle', a: '0.5', b: '0.8', fixture: 'chord' },
		{ kernel: 'SHIMMER', axis: 'sparkle', a: '0.5', b: '0.8', fixture: 'house-124' },
		{ kernel: 'STUTTER', axis: 'chunk', a: '1/4', b: '1/8', fixture: 'house-124' },
		{ kernel: 'STUTTER', axis: 'chunk', a: '1/4', b: '1/8', fixture: 'vocal' },
		{ kernel: 'STUTTER', axis: 'blend', a: 'equal-power', b: 'dry-held', fixture: 'chord' },
		{ kernel: 'STUTTER', axis: 'blend', a: 'equal-power', b: 'dry-held', fixture: 'house-124' }
	);
	return out;
}

function _variantTable(kernel, axis) {
	if (kernel === 'PITCH') return VARIANTS.PITCH;
	return /** @type {any} */ (VARIANTS[kernel])[axis];
}

/** @param {any} manifest */
export function assertBufferSpacePairsManifestShape(manifest) {
	const fail = (field) => {
		throw new Error(`buffer-space pairs manifest shape: ${field}`);
	};
	if (typeof manifest?.family !== 'string' || manifest.family !== 'buffer-space')
		fail('family must be buffer-space');
	if (!Number.isInteger(manifest.round) || manifest.round < 1)
		fail('round must be an integer >= 1');
	if (!Number.isInteger(manifest.seed) || manifest.seed < 0 || manifest.seed > 0xffffffff)
		fail('seed must be a uint32');
	if (
		typeof manifest.renderer?.id !== 'string' ||
		!manifest.renderer.id ||
		typeof manifest.renderer.real_path !== 'boolean'
	)
		fail('renderer must be {id: non-empty string, real_path: boolean}');
	if (!manifest.variants || typeof manifest.variants !== 'object')
		fail('variants sidecar is required');
	if (!Array.isArray(manifest.pairs) || manifest.pairs.length !== 16)
		fail('pairs must hold 16 items');
	for (const kernel of KERNELS) {
		const n = manifest.pairs.filter((p) => p.meta?.kernel === kernel).length;
		if (n !== PAIR_COUNTS[kernel])
			fail(`kernel ${kernel} must have ${PAIR_COUNTS[kernel]} pairs, got ${n}`);
	}
	const ids = new Set();
	for (const p of manifest.pairs) {
		if (typeof p.id !== 'string' || ids.has(p.id)) fail(`pair id ${p.id} must be unique`);
		ids.add(p.id);
		if (typeof p.a !== 'string' || typeof p.b !== 'string' || p.a === p.b)
			fail(`pair ${p.id} a/b distinct`);
		if (typeof p.question !== 'string') fail(`pair ${p.id} question`);
		const m = p.meta;
		if (!m || !KERNELS.includes(m.kernel)) fail(`pair ${p.id} meta.kernel`);
		if (!m.variant_setting || typeof m.variant_setting !== 'string')
			fail(`pair ${p.id} variant_setting`);
		if (m.a_variant === m.b_variant) fail(`pair ${p.id} variants must differ`);
		const table = _variantTable(m.kernel, m.axis);
		if (!(m.a_variant in table) || !(m.b_variant in table))
			fail(`pair ${p.id} unknown variant keys`);
		if (!FIXTURE_NAMES.includes(m.fixture)) fail(`pair ${p.id} fixture`);
		if (!Number.isFinite(m.lufs_a) || !Number.isFinite(m.lufs_b)) fail(`pair ${p.id} lufs`);
		if (!Number.isFinite(m.lufs_delta_db) || m.lufs_delta_db > LUFS_MATCH_MAX_DB + 1e-6)
			fail(`pair ${p.id} lufs_delta_db`);
		if (!m.variant_key || typeof m.variant_key !== 'object')
			fail(`pair ${p.id} variant_key sidecar`);
	}
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
			const kind = `${fmt.format}/${fmt.bits}`;
			if (!['1/16', '1/24', '3/32'].includes(kind) || fmt.ch < 1 || fmt.ch > 2)
				throw new Error(`${path} unsupported format ${kind}`);
			if (fmt.rate !== sr) throw new Error(`${path} sample rate ${fmt.rate} != ${sr}`);
			const bytes = fmt.bits / 8;
			const frames = Math.floor(size / (bytes * fmt.ch));
			if (frames < n) throw new Error(`${path} has ${frames} frames, needs ${n}`);
			const read = (p) =>
				kind === '1/16'
					? buf.readInt16LE(p) / 32768
					: kind === '1/24'
						? buf.readIntLE(p, 3) / 8388608
						: buf.readFloatLE(p);
			return [0, 1].map((c) =>
				Float64Array.from({ length: n }, (_, i) =>
					read(off + 8 + (i * fmt.ch + Math.min(c, fmt.ch - 1)) * bytes)
				)
			);
		}
		off += 8 + size + (size % 2);
	}
	throw new Error(`${path} has no data chunk`);
}

//----------------------------------------------------------------------------- export
/**
 * @param {{outDir:string, renderer:Renderer, allowTestDouble?:boolean, seed?:number, sampleRate?:number, bpm?:number, bars?:number, fixtures?:Record<string,string>}} opts
 */
export async function exportBufferSpaceFxPairs({
	outDir,
	renderer,
	allowTestDouble = false,
	seed = DEFAULT_SEED,
	sampleRate = SAMPLE_RATE,
	bpm = BPM,
	bars = CLIP_BARS,
	fixtures
}) {
	if (
		typeof renderer?.id !== 'string' ||
		typeof renderer.realPath !== 'boolean' ||
		typeof renderer.render !== 'function'
	)
		throw new Error('renderer {id, realPath, render} is required');
	if (renderer.realPath !== true && !allowTestDouble) {
		throw new Error(
			`renderer ${renderer.id} is not a real-path renderer; pass allowTestDouble only from tests`
		);
	}
	if (!outDir) throw new Error('--out <dir> is required');
	if (existsSync(outDir) && readdirSync(outDir).length > 0)
		throw new Error(`--out ${outDir} is not empty; refusing to overwrite an earlier rating round`);
	seed >>>= 0;
	const sr = sampleRate;
	const n = _beatToSample(bars * BEATS_PER_BAR, bpm, sr);
	const fx = _buildFixtures(n, sr, bpm, seed, fixtures);
	const fixtureByName = Object.fromEntries(fx.map((f) => [f.name, f]));
	const plan = canonicalPairs();
	const next = _rng(seed);
	_shuffle(plan, next);
	plan.forEach((c, idx) => {
		c.id = `p${String(idx + 1).padStart(2, '0')}`;
		c.swap = next() < 0.5;
	});

	const cache = new Map();
	const renderKey = (spec, variant) =>
		`${spec.kernel}|${spec.axis}|${variant}|${spec.fixture}|${spec.pitchSemitones ?? ''}`;

	const clips = [];
	const pairs = [];
	for (const spec of plan) {
		const [aVar, bVar] = spec.swap ? [spec.b, spec.a] : [spec.a, spec.b];
		const rendered = {};
		for (const variant of [aVar, bVar]) {
			const key = renderKey(spec, variant);
			if (!cache.has(key)) {
				const table = _variantTable(spec.kernel, spec.axis);
				const constants = table[variant];
				const input = fixtureByName[spec.fixture].channels;
				const out = await renderer.render({
					kernel: spec.kernel,
					axis: spec.axis,
					variant,
					constants,
					input,
					sampleRate: sr,
					bpm,
					seed,
					depth: DEPTH,
					pitchSemitones: spec.pitchSemitones
				});
				if (!Array.isArray(out) || out.length !== 2 || out[0].length !== n)
					throw new Error(`renderer ${renderer.id} broke the contract for ${key}`);
				_checkFiniteAndPeak(out, {
					file: `clips/${spec.id}-raw.wav`,
					kernel: spec.kernel,
					variant
				});
				cache.set(key, out);
			}
			rendered[variant] = cache.get(key);
		}
		const matched = levelMatchPair(rendered[aVar], rendered[bVar], sr);
		_checkFiniteAndPeak(matched.a, {
			file: `clips/${spec.id}-a.wav`,
			kernel: spec.kernel,
			variant: aVar
		});
		_checkFiniteAndPeak(matched.b, {
			file: `clips/${spec.id}-b.wav`,
			kernel: spec.kernel,
			variant: bVar
		});
		const aPath = `clips/${spec.id}-a.wav`;
		const bPath = `clips/${spec.id}-b.wav`;
		clips.push([aPath, matched.a], [bPath, matched.b]);
		const setting = _variantTable(spec.kernel, spec.axis)[aVar].setting;
		pairs.push({
			id: spec.id,
			a: aPath,
			b: bPath,
			question: QUESTION,
			meta: {
				kernel: spec.kernel,
				axis: spec.axis,
				fixture: spec.fixture,
				variant_setting: setting,
				a_variant: aVar,
				b_variant: bVar,
				variant_key: { a: aVar, b: bVar },
				pitch_semitones: spec.pitchSemitones ?? null,
				lufs_a: matched.lufs_a,
				lufs_b: matched.lufs_b,
				lufs_delta_db: matched.lufs_delta_db,
				match_gain_db: matched.match_gain_db,
				depth: DEPTH
			}
		});
	}

	const manifest = {
		family: 'buffer-space',
		round: 1,
		seed,
		generated_by: GENERATED_BY,
		renderer: { id: renderer.id, real_path: renderer.realPath },
		sample_rate: sr,
		bpm,
		bars,
		samples: n,
		lufs_match_max_db: LUFS_MATCH_MAX_DB,
		fixtures: fx.map(({ name, source, dry_rms_dbfs, peak_dbfs }) => ({
			name,
			source,
			dry_rms_dbfs,
			peak_dbfs
		})),
		variants: VARIANTS,
		pair_counts: PAIR_COUNTS,
		vetoes_applied: ['finite', 'peak'],
		vetoes_not_applied: VETOES_NOT_APPLIED,
		pairs
	};
	assertBufferSpacePairsManifestShape(manifest);
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
		else if (flag === '--sample-rate') opts.sampleRate = Number(val);
		else if (flag === '--bars') opts.bars = Number(val);
		else if (flag === '--fixture') {
			const eq = val.indexOf('=');
			if (eq <= 0) throw new Error(`--fixture expects <name>=<path.wav>, got ${val}`);
			opts.fixtures[val.slice(0, eq)] = val.slice(eq + 1);
		} else throw new Error(`unknown argument ${flag}`);
	}
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
		const m = await exportBufferSpaceFxPairs({ ...opts, renderer });
		console.log(
			`[OK] wrote ${m.pairs.length} pairs, ${m.pairs.length * 2} clips, manifest ${join(opts.outDir, 'pairs.json')}, seed ${m.seed}`
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
