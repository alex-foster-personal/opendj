#!/usr/bin/env node
/**
 * Export blind A/B pairs for the ROLL beat FX level law and the short-roll seam edge (#9845).
 *
 * Spec: (private path) sections 4 and 6 (taste feature,
 * calibrated in #9846 on the shared #9776 blind A/B page).
 *
 * Calibration clips render ONLY through a real-path renderer: the #9774 fx render harness driving
 * the #9793 ROLL kernel and the #9840 deck roll. Until those land and an adapter is wired into
 * resolveRealRenderer, the CLI prints [UNAVAILABLE], exits 2 and writes nothing. This script holds
 * no ROLL DSP of its own; pairing, blinding, measures, loudness match, vetoes and the WAV writer
 * run behind the Renderer interface below.
 *
 * Pairs: every comparison is anchored on the spec's fallback (equal-power law, 1 ms edge) against
 * each other candidate. Level law at knob 0.5 and at the renderer's defaultKnob (4 pairs); seam edge
 * on 1/16 and 1/32 beat deck rolls (4 pairs). A deck roll is 100 % wet with no mix knob, so its
 * manifest knob is 1. Each pair is shown twice with sides swapped: 16 trials.
 *
 * Measures (raw render, before loudness match):
 *   wet_dry_db        how much louder or quieter the rolled section is than the dry programme it
 *                     replaces (RMS over the roll section, dB).
 *   seam_click_ratio  worst repeat seam: peak second difference within SEAM_WINDOW_MS of a seam,
 *                     over the dry programme's peak second difference in the same section.
 *
 * Usage:
 *   node apps/webui/frontend/scripts/export-roll-rating-pairs.mjs --out <dir> [--seed <uint32>]
 *       [--sample-rate <hz>] [--fixture house-124=<path.wav>]
 *
 * Mini-PRD:
 *   ✔︎ ✅ 🎯 R0 no real render path means UNAVAILABLE, never a calibration from a reimplementation
 *     [if] any real-path dependency file is missing [then ⛔️ unless the CLI exits 2 with [UNAVAILABLE] naming each missing issue]
 *     [if] UNAVAILABLE [then ⛔️ if --out was created or written]
 *     [if] a renderer with realPath !== true arrives without allowTestDouble [then ⛔️ unless it throws]
 *   ✔︎ ✅ 🎯 R1 writes 16 loudness-matched 8-bar WAVs plus pairs.json (#9776 shape): 8 pairs, 16 trials, sides from a recorded seed
 *     [if] same seed twice [then ⛔️ if pairs.json differs by one byte]
 *     [if] a pair's two trials are not mirrored [then ⛔️]
 *     [if] a clip RMS is off TARGET_RMS_DBFS by more than 0.1 dB [then ⛔️]
 *   ✔︎ ✅ 🎯 R2 every trial names both variant ids, the fixture, the knob, roll length, wet_dry_db and seam_click_ratio
 *     [if] a measure is missing or non-finite [then ⛔️]
 *     [if] a variant id is not a key of variants[feature] [then ⛔️]
 *     [if] a variant id appears in a clip path or the question [then ⛔️]
 *   ✔︎ ✅ 🎯 R3 every written clip passes the hard vetoes (finite, peak) or nothing is written and the file is named
 *     [if] a clip exceeds 0 dBFS [then ⛔️ unless VetoError peak naming file=clips/pNN-K.wav and an empty out dir]
 *     [if] a render holds NaN [then ⛔️ unless VetoError finite naming the file]
 *     [if] main() hits a veto [then ⛔️ unless it returns 1 with an [ERROR] line naming the file]
 *   (R1-R3 are exercised under a test double only; they carry no real-path evidence yet.)
 */
import { mkdirSync, readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

/**
 * @typedef {{ feature: 'level-law'|'seam-edge', variant: string, constants: object,
 *   input: Float64Array[], sampleRate: number, bpm: number, seed: number,
 *   knob: number, rollBeats: number, onSample: number, offSample: number }} RenderRequest
 * @typedef {{ out: Float64Array[], seams: number[] }} RenderResult
 *   out: stereo render of the whole clip. seams: sample index of every repeat boundary
 *   inside [onSample, offSample), ascending.
 * @typedef {{ id: string, realPath: boolean, defaultKnob: number,
 *   render(req: RenderRequest): Promise<RenderResult> }} Renderer
 */

//----------------------------------------------------------------------------- CFG
export const DEFAULT_SEED = 0x5eed9845;
export const SAMPLE_RATE = 48000;
export const BPM = 124; // spec section 6: 4/4 house at 124 BPM
export const CLIP_BARS = 8; // issue: 8 bars each
export const BEATS_PER_BAR = 4;
export const ROLL_ON_BEAT = 8; // roll engages at bar 3 beat 1 ...
export const ROLL_OFF_BEAT = 24; // ... and releases at bar 7 beat 1 (dry context either side)
export const LEVEL_LAW_ROLL_BEATS = 0.25;
export const SHORT_ROLL_BEATS = [1 / 16, 1 / 32];
export const MID_KNOB = 0.5;
export const TARGET_RMS_DBFS = -23;
export const DRY_RMS_DBFS = -23;
export const FIXTURE_PEAK_MAX_DBFS = -3;
export const SEAM_WINDOW_MS = 1;
export const TRIALS_PER_PAIR = 2;
export const FIXTURE_NAME = 'house-124';
export const QUESTION = 'Which roll sounds better?';
export const EXIT_UNAVAILABLE = 2;
export const REAL_PATH_DEPENDENCIES = Object.freeze([
	{
		issue: '#9774',
		what: 'fx render harness',
		path: 'apps/webui/frontend/tests/e2e/support/fx-render-harness.ts'
	},
	{
		issue: '#9793',
		what: 'ROLL beat FX kernel',
		path: 'apps/webui/frontend/src/lib/rb/roll-fx-kernel.ts'
	},
	{
		issue: '#9840',
		what: 'deck roll command',
		path: 'apps/webui/frontend/src/lib/player/transport/roll-state.ts'
	}
]);
export const VARIANTS = Object.freeze({
	'level-law': {
		'equal-gain': { law: 'equal-gain' },
		'equal-power': { law: 'equal-power' },
		'hold-3db': { law: 'hold', holdDb: -3, holdUntil: 0.75 }
	},
	'seam-edge': {
		hard: { edgeMs: 0 },
		'edge-1ms': { edgeMs: 1 },
		'edge-3ms': { edgeMs: 3 }
	}
});
export const ANCHOR = Object.freeze({ 'level-law': 'equal-power', 'seam-edge': 'edge-1ms' });
const REPO_ROOT = fileURLToPath(new URL('../../../../', import.meta.url));
const GENERATED_BY = 'apps/webui/frontend/scripts/export-roll-rating-pairs.mjs';
const FLT_MIN_NORMAL = 1.1754943508222875e-38;
const VETOES_APPLIED = ['finite', 'peak'];
const VETOES_NOT_APPLIED = [
	{
		veto: 'click-ratio-1.5',
		reason:
			'spec section 6 exempts 1/16 and 1/32 rolls (their ceiling is the edge chosen in #9846); engage, release and 1/8+ seams are checked by #9793 / #9842 render checks'
	},
	{ veto: 'added-latency', reason: "measured by the #9774 harness in the kernel's own PR" },
	{
		veto: 'detent-bit-exact',
		reason: "knob 0 / off is measured by the #9774 harness in the kernel's own PR"
	},
	{ veto: 'no-runaway', reason: "measured by the #9774 harness in the kernel's own PR" }
];

export class VetoError extends Error {
	/** @param {{veto:string,file:string,feature:string,variant:string,condition:string,measured:number,limit:number|string}} f */
	constructor(f) {
		super(
			`veto ${f.veto} failed: file=${f.file} feature=${f.feature} variant=${f.variant} ${f.condition} measured=${f.measured} limit=${f.limit}`
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
/** The only source of a calibration renderer. Throws UnavailableError until the harness adapter exists. */
export function resolveRealRenderer(repoRoot = REPO_ROOT) {
	const missing = REAL_PATH_DEPENDENCIES.filter((d) => !existsSync(join(repoRoot, d.path)));
	if (missing.length > 0) {
		const names = missing.map((d) => `${d.issue} ${d.what} (${d.path})`).join(', ');
		throw new UnavailableError(
			`UNAVAILABLE: roll calibration renders only through the real kernel and deck roll; missing ${names}`,
			missing
		);
	}
	throw new UnavailableError(
		'UNAVAILABLE: the #9774 harness, #9793 kernel and #9840 deck roll are present but this script has no adapter to them yet; wire resolveRealRenderer',
		[]
	);
}

//----------------------------------------------------------------------------- helpers
/** Seeded xorshift32, same algorithm as scripts/export-sound-color-pairs.mjs. Returns [0, 1). */
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
const _round = (x, dp) => Math.round(x * 10 ** dp) / 10 ** dp + 0; // + 0 turns -0 into 0
function _rms(channels, from = 0, to = channels[0].length) {
	let sum = 0;
	for (const ch of channels) for (let i = from; i < to; i++) sum += ch[i] * ch[i];
	return Math.sqrt(sum / (channels.length * (to - from)));
}
function _shuffle(items, next) {
	for (let i = items.length - 1; i > 0; i--) {
		const j = Math.floor(next() * (i + 1));
		[items[i], items[j]] = [items[j], items[i]];
	}
}
const _id = (prefix, idx) => `${prefix}${String(idx + 1).padStart(2, '0')}`;
/** Sample index of a beat position; clip length, roll on and roll off all derive from this. */
const _beatToSample = (beats, bpm, sr) => Math.round(((beats * 60) / bpm) * sr);

//----------------------------------------------------------------------------- fixture
// Fixture INPUT synthesis only; never on a render path.
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
function _buildFixture(n, sr, bpm, seed, overrides) {
	const path = overrides?.[FIXTURE_NAME];
	if (path) return _normaliseFixture(FIXTURE_NAME, 'wav', _readWav(path, sr, n));
	return _normaliseFixture(FIXTURE_NAME, 'synthesized', _house124(n, sr, bpm, seed));
}

//----------------------------------------------------------------------------- measures
/** Port of secondDiffPeak in the COLORFX-03 harness (color-fx-harness.ts on af--b2b-link). */
function _secondDiffPeak(channels, from, to) {
	let m = 0;
	for (const ch of channels) {
		for (let i = Math.max(2, from); i < Math.min(ch.length, to); i++)
			m = Math.max(m, Math.abs(ch[i] - 2 * ch[i - 1] + ch[i - 2]));
	}
	return m;
}
function _measure(render, dry, onSample, offSample, sr) {
	const w = Math.round((SEAM_WINDOW_MS * sr) / 1000);
	const dryRef = _secondDiffPeak(dry, onSample, offSample);
	if (dryRef === 0)
		throw new Error(
			'dry fixture has zero second difference over the roll section; seam_click_ratio is undefined'
		);
	const worstSeam = Math.max(...render.seams.map((s) => _secondDiffPeak(render.out, s - w, s + w)));
	return {
		wet_dry_db: _round(
			20 * Math.log10(_rms(render.out, onSample, offSample) / _rms(dry, onSample, offSample)),
			2
		),
		seam_click_ratio: _round(worstSeam / dryRef, 3)
	};
}

//----------------------------------------------------------------------------- vetoes
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
function _checkFiniteAndPeak(channels, ctx) {
	_checkFinite(channels, ctx);
	for (const ch of channels)
		for (const x of ch)
			if (Math.abs(x) > 1)
				throw new VetoError({ veto: 'peak', ...ctx, measured: Math.abs(x), limit: 1 });
}
function _checkContract(renderer, result, n, onSample, offSample) {
	const broke = (why) => {
		throw new Error(`renderer ${renderer.id} broke the contract: ${why}`);
	};
	if (
		!Array.isArray(result?.out) ||
		result.out.length !== 2 ||
		!result.out.every((ch) => ch instanceof Float64Array && ch.length === n)
	)
		broke(`out must be two Float64Array of length ${n}`);
	const s = result.seams;
	if (!Array.isArray(s) || s.length === 0) broke('seams must be a non-empty array');
	s.forEach((x, i) => {
		if (!Number.isInteger(x) || x < onSample || x >= offSample || (i > 0 && x <= s[i - 1]))
			broke(`seams must be ascending integers inside [${onSample}, ${offSample})`);
	});
}

//----------------------------------------------------------------------------- pairs and manifest
/** The 8 canonical comparisons: each feature's anchor against each other variant, per condition. */
function _canonicalPairs(defaultKnob) {
	const conditions = {
		'level-law': [MID_KNOB, defaultKnob].map((knob) => ({ knob, rollBeats: LEVEL_LAW_ROLL_BEATS })),
		'seam-edge': SHORT_ROLL_BEATS.map((rollBeats) => ({ knob: 1, rollBeats }))
	};
	const pairs = [];
	for (const feature of Object.keys(VARIANTS)) {
		const others = Object.keys(VARIANTS[feature]).filter((v) => v !== ANCHOR[feature]);
		for (const cond of conditions[feature])
			for (const other of others)
				pairs.push({ feature, ...cond, variants: [ANCHOR[feature], other] });
	}
	return pairs;
}
const _renderKey = (feature, knob, rollBeats, variant) =>
	`${feature}|${knob}|${rollBeats}|${variant}`;

/** @param {any} manifest */
export function assertRollPairsManifestShape(manifest) {
	const fail = (field) => {
		throw new Error(`roll pairs manifest shape: ${field}`);
	};
	if (typeof manifest?.family !== 'string' || !manifest.family)
		fail('family must be a non-empty string');
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
	if (!Array.isArray(manifest.pairs) || manifest.pairs.length === 0)
		fail('pairs must be a non-empty array');
	const pairCount = Object.values(VARIANTS).reduce(
		(acc, v) => acc + (Object.keys(v).length - 1) * 2,
		0
	);
	if (manifest.pairs.length !== pairCount * TRIALS_PER_PAIR)
		fail(`pairs must hold ${pairCount * TRIALS_PER_PAIR} trials`);
	const ids = new Set();
	const byPair = new Map();
	for (const p of manifest.pairs) {
		if (typeof p.id !== 'string' || ids.has(p.id)) fail(`trial id ${p.id} must be a unique string`);
		ids.add(p.id);
		if (typeof p.a !== 'string' || typeof p.b !== 'string' || p.a === p.b)
			fail(`trial ${p.id} a/b must be distinct strings`);
		if (typeof p.question !== 'string') fail(`trial ${p.id} question`);
		const m = p.meta;
		if (!m || !(m.feature in VARIANTS)) fail(`trial ${p.id} meta.feature`);
		const known = manifest.variants?.[m.feature] ?? {};
		if (m.a_variant === m.b_variant || !(m.a_variant in known) || !(m.b_variant in known))
			fail(`trial ${p.id} meta.a_variant/b_variant`);
		if (typeof m.fixture !== 'string' || !m.fixture) fail(`trial ${p.id} meta.fixture`);
		if (!(typeof m.knob === 'number' && m.knob > 0 && m.knob <= 1)) fail(`trial ${p.id} meta.knob`);
		const rollOk =
			m.feature === 'level-law'
				? m.roll_beats === LEVEL_LAW_ROLL_BEATS
				: SHORT_ROLL_BEATS.includes(m.roll_beats);
		if (!rollOk) fail(`trial ${p.id} meta.roll_beats`);
		for (const key of ['wet_dry_db', 'seam_click_ratio', 'match_gain_db']) {
			for (const v of [m.a_variant, m.b_variant])
				if (!Number.isFinite(m[key]?.[v])) fail(`trial ${p.id} meta.${key}.${v}`);
		}
		if (!byPair.has(m.pair_id)) byPair.set(m.pair_id, []);
		byPair.get(m.pair_id).push(p);
	}
	if (byPair.size !== pairCount) fail(`pairs must cover ${pairCount} distinct pair_id`);
	for (const [pid, trials] of byPair) {
		const [t1, t2] = trials;
		if (
			trials.length !== TRIALS_PER_PAIR ||
			t1.a !== t2.b ||
			t1.b !== t2.a ||
			t1.meta.a_variant !== t2.meta.b_variant
		)
			fail(`pair ${pid} must appear in ${TRIALS_PER_PAIR} mirrored trials`);
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
				throw new Error(
					`${path} format ${kind} with ${fmt.ch} channels is unsupported (16/24-bit PCM or 32-bit float, mono or stereo)`
				);
			if (fmt.rate !== sr)
				throw new Error(`${path} sample rate ${fmt.rate} != --sample-rate ${sr}`);
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
 * @param {{outDir:string, renderer:Renderer, allowTestDouble?:boolean, seed?:number, sampleRate?:number, bpm?:number, fixtures?:Record<string,string>}} opts
 * @returns {Promise<any>} the manifest written to <outDir>/pairs.json
 */
export async function exportRollRatingPairs({
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
		throw new Error('renderer {id, realPath, defaultKnob, render} is required');
	if (renderer.realPath !== true && !allowTestDouble)
		throw new Error(
			`renderer ${renderer.id} is not a real-path renderer; a calibration export needs the #9774 harness (pass allowTestDouble only from tests)`
		);
	const defaultKnob = renderer.defaultKnob;
	if (!(
		Number.isFinite(defaultKnob) &&
		defaultKnob > 0 &&
		defaultKnob < 1 &&
		defaultKnob !== MID_KNOB
	))
		throw new Error(
			`renderer ${renderer.id} defaultKnob must be finite, inside (0, 1) and not ${MID_KNOB}; got ${defaultKnob}`
		);
	if (!outDir) throw new Error('--out <dir> is required');
	if (existsSync(outDir) && readdirSync(outDir).length > 0)
		throw new Error(`--out ${outDir} is not empty; refusing to overwrite an earlier rating round`);
	seed >>>= 0;
	const sr = sampleRate;
	const n = _beatToSample(CLIP_BARS * BEATS_PER_BAR, bpm, sr);
	const onSample = _beatToSample(ROLL_ON_BEAT, bpm, sr);
	const offSample = _beatToSample(ROLL_OFF_BEAT, bpm, sr);
	const fixture = _buildFixture(n, sr, bpm, seed, fixtures);
	const dry = fixture.channels;

	const canonical = _canonicalPairs(defaultKnob);
	// Seeded blinding, one stream, fixed order: pair order, file sides, trial-1 sides, trial order.
	const next = _rng(seed);
	_shuffle(canonical, next);
	canonical.forEach((c, idx) => (c.id = _id('p', idx)));
	for (const c of canonical) c.files = next() < 0.5 ? c.variants : [c.variants[1], c.variants[0]];
	for (const c of canonical) c.firstSwap = next() < 0.5;

	// First clip that uses each render, so a raw-render veto names a file.
	const firstFile = new Map();
	for (const c of canonical) {
		c.files.forEach((v, k) => {
			const key = _renderKey(c.feature, c.knob, c.rollBeats, v);
			if (!firstFile.has(key)) firstFile.set(key, `clips/${c.id}-${k + 1}.wav`);
		});
	}

	// Render and measure each distinct (feature, condition, variant) once, all in memory.
	const renders = new Map();
	for (const c of canonical) {
		for (const variant of c.variants) {
			const key = _renderKey(c.feature, c.knob, c.rollBeats, variant);
			if (renders.has(key)) continue;
			const req = {
				feature: c.feature,
				variant,
				constants: VARIANTS[c.feature][variant],
				input: dry,
				sampleRate: sr,
				bpm,
				seed,
				knob: c.knob,
				rollBeats: c.rollBeats,
				onSample,
				offSample
			};
			const result = await renderer.render(req);
			_checkContract(renderer, result, n, onSample, offSample);
			_checkFinite(result.out, {
				file: firstFile.get(key),
				feature: c.feature,
				variant,
				condition: `knob=${c.knob} roll_beats=${c.rollBeats}`
			});
			renders.set(key, { out: result.out, ..._measure(result, dry, onSample, offSample, sr) });
		}
	}

	// Loudness match and veto every clip before anything is written.
	const clips = [];
	for (const c of canonical) {
		c.match = {};
		c.paths = c.files.map((variant, k) => {
			const file = `clips/${c.id}-${k + 1}.wav`;
			const r = renders.get(_renderKey(c.feature, c.knob, c.rollBeats, variant));
			const gainDb = TARGET_RMS_DBFS - 20 * Math.log10(_rms(r.out));
			const gain = _dbToLin(gainDb);
			const matched = r.out.map((ch) => Float64Array.from(ch, (x) => x * gain));
			_checkFiniteAndPeak(matched, {
				file,
				feature: c.feature,
				variant,
				condition: `knob=${c.knob} roll_beats=${c.rollBeats}`
			});
			c.match[variant] = _round(gainDb, 2);
			clips.push([file, matched]);
			return file;
		});
	}

	const trials = [];
	for (const c of canonical) {
		const order = c.firstSwap ? [1, 0] : [0, 1];
		for (let t = 0; t < TRIALS_PER_PAIR; t++) {
			const [ai, bi] = t === 0 ? order : [order[1], order[0]];
			const pick = (key) =>
				Object.fromEntries(
					[c.files[ai], c.files[bi]].map((v) => [
						v,
						renders.get(_renderKey(c.feature, c.knob, c.rollBeats, v))[key]
					])
				);
			trials.push({
				a: c.paths[ai],
				b: c.paths[bi],
				question: QUESTION,
				meta: {
					pair_id: c.id,
					trial: t + 1,
					feature: c.feature,
					fixture: fixture.name,
					knob: c.knob,
					roll_beats: c.rollBeats,
					a_variant: c.files[ai],
					b_variant: c.files[bi],
					wet_dry_db: pick('wet_dry_db'),
					seam_click_ratio: pick('seam_click_ratio'),
					match_gain_db: {
						[c.files[ai]]: c.match[c.files[ai]],
						[c.files[bi]]: c.match[c.files[bi]]
					}
				}
			});
		}
	}
	_shuffle(trials, next);
	const pairs = trials.map((t, idx) => ({ id: _id('t', idx), ...t }));

	const manifest = {
		family: 'roll',
		round: 1,
		seed,
		generated_by: GENERATED_BY,
		renderer: { id: renderer.id, real_path: renderer.realPath },
		sample_rate: sr,
		bpm,
		bars: CLIP_BARS,
		samples: n,
		roll_on_beat: ROLL_ON_BEAT,
		roll_off_beat: ROLL_OFF_BEAT,
		target_rms_dbfs: TARGET_RMS_DBFS,
		fixtures: [
			{
				name: fixture.name,
				source: fixture.source,
				dry_rms_dbfs: fixture.dry_rms_dbfs,
				peak_dbfs: fixture.peak_dbfs
			}
		],
		variants: VARIANTS,
		pair_count: canonical.length,
		trials_per_pair: TRIALS_PER_PAIR,
		vetoes_applied: VETOES_APPLIED,
		vetoes_not_applied: VETOES_NOT_APPLIED,
		pairs
	};
	assertRollPairsManifestShape(manifest);

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
		else if (flag === '--fixture') {
			const [name, path] = val.split('=');
			if (!name || !path) throw new Error(`--fixture expects <name>=<path.wav>, got ${val}`);
			if (name !== FIXTURE_NAME) throw new Error(`unknown fixture ${name}; only ${FIXTURE_NAME}`);
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
/**
 * @param {string[]} argv
 * @param {{renderer?: Renderer, allowTestDouble?: boolean}} [testOpts] tests only; the CLI entry passes none
 */
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
		const m = await exportRollRatingPairs({
			...opts,
			renderer,
			allowTestDouble: testOpts.allowTestDouble === true
		});
		console.log(
			`[OK] wrote ${m.pair_count} pairs, ${m.pairs.length} trials, ${m.pair_count * 2} clips, manifest ${join(opts.outDir, 'pairs.json')}, seed ${m.seed}`
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
