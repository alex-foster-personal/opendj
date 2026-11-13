#!/usr/bin/env node
/**
 * Export blind A/B pairs for the FILTER fast-turn and resonance voicing (#9829, FXFILT-9829), plus the beat FX
 * LFO FILTER shape and direction default (#10014).
 *
 * Spec: (private path) section 6, features B (resonance) and C (fast-turn).
 *
 * Calibration clips render ONLY through a real-path renderer: the #9774 fx render harness driving the
 * shipped deck filter graph with the #9826 resonance ceiling and the #9828 glide domain and time. Until
 * those land and an adapter is wired into resolveRealRenderer, the CLI prints [UNAVAILABLE], exits 2 and
 * writes nothing. This script holds no filter DSP of its own: glide domain, glide time, Q and slope are
 * fields of the render REQUEST, and the script only synthesizes fixtures and probes, measures the
 * returned buffers, vetoes, blinds and writes.
 *
 * Usage:
 *   node scripts/export-filter-rating-pairs.mjs --out <dir> [--seed <uint32>] [--seconds <n>]
 *       [--sample-rate <hz>] [--winning-glide hz|octave:<5|10|20>]
 *
 * Mini-PRD:
 *   ✔︎ ✅ 🎯 R0 no real render path means UNAVAILABLE, never a calibration from a reimplementation
 *     [if] any real-path dependency file is missing [then ⛔️ unless the CLI exits 2 with [UNAVAILABLE] naming each missing issue and path]
 *     [if] UNAVAILABLE [then ⛔️ if --out was created or written]
 *     [if] exportFilterRatingPairs gets a renderer with realPath !== true and no allowTestDouble [then ⛔️ unless it throws]
 *   ✔︎ ✅ R1 writes 10 pairs (glide hz/octave, glide 5/20 ms, Q 2/4 and 4/8 at the winning glide, 12/24 dB/oct once,
 *     LFO FILTER sine/triangle and down/up at one division)
 *     [if] same seed twice [then ⛔️ if manifests differ]
 *     [if] a candidate label shows in an id, clip path or question [then ⛔️]
 *   ✔︎ ✅ R2 each side records octave span at gesture end, HP residual at 100/160/250 Hz, corner peak and RMS change
 *     [if] a proxy is missing or non-finite [then ⛔️]
 *   ✔︎ ✅ R3 every render passes the finite and peak vetoes before any file is written
 *     [if] a clip exceeds 0 dBFS [then ⛔️ unless VetoError peak naming pair, side and sample, and nothing written]
 *   ✔︎ ✅ 🎯 R4 a render that resolves after the caller replaced the export (aborted its signal) is discarded, never written
 *     [if] signal aborts while a render is in flight and that render then resolves valid [then ⛔️ unless the export rejects and nothing is written]
 *   (R1-R3 are exercised under a test double only; they carry no real-path evidence yet.)
 */
import { mkdirSync, readdirSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

/**
 * @typedef {{t_ms: number, value: number}[]} Automation  knob points relative to clip start, linear between, held after the last
 * @typedef {{ sampleRate: number, input: Float32Array[], direction: 'hp'|'lp', automation: Automation,
 *   glide_domain: 'hz'|'octave', glide_ms: number, resonance_q_linear: number|null, slope_db_oct: 12|24,
 *   lfo_shape?: 'sine'|'triangle'|'saw_decay', lfo_direction?: 'down'|'up', lfo_beats?: number, lfo_bpm?: number }} RenderRequest
 *   a request carrying lfo_shape renders the #9792 LFO FILTER kernel at full level, knob held at the dry detent
 * @typedef {{ channels: Float32Array[], cornerHz: Float32Array, quantum: number }} RenderResult
 *   cornerHz is the corner frequency the real graph applied, one value per render quantum of `quantum` frames
 * @typedef {{ id: string, realPath: boolean, render(req: RenderRequest): Promise<RenderResult> }} Renderer
 */

//----------------------------------------------------------------------------- CFG
export const DEFAULT_SEED = 0x5eed9829;
export const SAMPLE_RATE = 48000;
export const CLIP_SECONDS = 4;
export const GESTURE_START_S = 0.5;
export const FLICK_MS = 150;
export const RIDE_MS = 2000;
export const FAMILY = 'filter';
export const ROUND = 1;
export const QUESTION = 'Which sounds more like a DJ throwing the filter?';
export const EXIT_UNAVAILABLE = 2;
export const DRY_RMS_DBFS = -23;
export const FIXTURE_PEAK_MAX_DBFS = -3;
export const FIXTURES = ['four-on-the-floor', 'vocal-led'];
// Shipped filter, the baseline every arm departs from (06-filter.md section 6).
export const SHIPPED = Object.freeze({ glide_domain: 'hz', glide_ms: 10, resonance_q_linear: null, slope_db_oct: 12 });
// "At the winning glide": #9830 has not voted, so the Q and slope pairs render at this constant.
// Re-export with --winning-glide <domain>:<ms> once #9830 picks; the manifest records what was used.
export const WINNING_GLIDE = Object.freeze({ glide_domain: 'octave', glide_ms: 10 });
// Knob endpoints, for octave-span normalisation only. Not DSP.
export const HP_FLOOR_HZ = 30;
export const HP_CEILING_HZ = 6300;
export const LP_CEILING_HZ = 20000;
export const LP_FLOOR_HZ = 130;
export const RESIDUAL_HZ = [100, 160, 250];
export const PROBE_TONE_DBFS = -20;
export const STATIC_PROBE_KNOB = Object.freeze({ hp: 0.75, lp: 0.25 });
export const MAX_PAIRS = 10;
// LFO FILTER pairs (#10014): the kernel default every LFO arm departs from, at one division on the fixture tempo.
export const LFO_SHIPPED = Object.freeze({ lfo_shape: 'sine', lfo_direction: 'down', lfo_beats: 1 });
export const FIXTURE_BPM = Object.freeze({ 'four-on-the-floor': 128, 'vocal-led': 124 });
export const LFO_QUESTION = 'Which filter movement sits better in the groove?';
const LFO_AXES = ['lfo_shape', 'lfo_direction'];
export const REAL_PATH_DEPENDENCIES = Object.freeze([
	{ issue: '#9774', what: 'fx render harness', path: 'apps/webui/frontend/tests/e2e/support/fx-render-harness.ts' },
	{ issue: '#9826', what: 'filter resonance ceiling', path: 'apps/webui/frontend/src/lib/rb/filter-resonance.ts' },
	{ issue: '#9828', what: 'filter glide domain and time', path: 'apps/webui/frontend/src/lib/rb/filter-glide.ts' },
	{ issue: '#9776', what: 'A/B page manifest schema', path: 'tools/fx-ab/manifest.schema.json' }
]);
const REPO_ROOT = fileURLToPath(new URL('..', import.meta.url));
const ANALYSIS_S = 1;
const RESIDUAL_WINDOW_MS = 100;
const DB_FLOOR = -120;

export const AXES = Object.freeze([
	{ axis: 'glide_domain', values: ['hz', 'octave'] },
	{ axis: 'glide_time', values: ['5ms', '20ms'] },
	{ axis: 'resonance_q', values: ['q2', 'q4', 'q8'] },
	{ axis: 'slope', values: ['12db', '24db'] },
	{ axis: 'lfo_shape', values: ['sine', 'triangle'] },
	{ axis: 'lfo_direction', values: ['down', 'up'] }
]);
export const REQUIRED_COMPARISONS = Object.freeze([
	{ axis: 'glide_domain', a: 'hz', b: 'octave' },
	{ axis: 'glide_time', a: '5ms', b: '20ms' },
	{ axis: 'resonance_q', a: 'q2', b: 'q4' },
	{ axis: 'resonance_q', a: 'q4', b: 'q8' },
	{ axis: 'slope', a: '12db', b: '24db' },
	{ axis: 'lfo_shape', a: 'sine', b: 'triangle' },
	{ axis: 'lfo_direction', a: 'down', b: 'up' }
]);
// Request overrides per candidate label. `winning` candidates sit on the winning glide, the rest on SHIPPED.
const CANDIDATES = {
	hz: { winning: false, set: { glide_domain: 'hz' } },
	octave: { winning: false, set: { glide_domain: 'octave' } },
	'5ms': { winning: true, set: { glide_ms: 5 } },
	'20ms': { winning: true, set: { glide_ms: 20 } },
	q2: { winning: true, set: { resonance_q_linear: 2 } },
	q4: { winning: true, set: { resonance_q_linear: 4 } },
	q8: { winning: true, set: { resonance_q_linear: 8 } },
	'12db': { winning: true, set: { slope_db_oct: 12 } },
	'24db': { winning: true, set: { slope_db_oct: 24 } },
	sine: { winning: false, set: { lfo_shape: 'sine' } },
	triangle: { winning: false, set: { lfo_shape: 'triangle' } },
	down: { winning: false, set: { lfo_direction: 'down' } },
	up: { winning: false, set: { lfo_direction: 'up' } }
};
export const PAIRS = Object.freeze([
	{ key: 'p1', axis: 'glide_domain', a: 'hz', b: 'octave', direction: 'hp', gesture: 'flick', fixture: 'four-on-the-floor' },
	{ key: 'p2', axis: 'glide_domain', a: 'hz', b: 'octave', direction: 'lp', gesture: 'flick', fixture: 'vocal-led' },
	{ key: 'p3', axis: 'glide_time', a: '5ms', b: '20ms', direction: 'hp', gesture: 'flick', fixture: 'vocal-led' },
	{ key: 'p4', axis: 'glide_time', a: '5ms', b: '20ms', direction: 'lp', gesture: 'flick', fixture: 'four-on-the-floor' },
	{ key: 'p5', axis: 'resonance_q', a: 'q2', b: 'q4', direction: 'hp', gesture: 'ride', fixture: 'four-on-the-floor' },
	{ key: 'p6', axis: 'resonance_q', a: 'q4', b: 'q8', direction: 'lp', gesture: 'ride', fixture: 'vocal-led' },
	{ key: 'p7', axis: 'resonance_q', a: 'q4', b: 'q8', direction: 'hp', gesture: 'flick', fixture: 'vocal-led' },
	{ key: 'p8', axis: 'slope', a: '12db', b: '24db', direction: 'lp', gesture: 'ride', fixture: 'four-on-the-floor' },
	{ key: 'p9', axis: 'lfo_shape', a: 'sine', b: 'triangle', direction: 'lp', gesture: 'ride', fixture: 'four-on-the-floor' },
	{ key: 'p10', axis: 'lfo_direction', a: 'down', b: 'up', direction: 'lp', gesture: 'ride', fixture: 'vocal-led' }
].map(Object.freeze));
const GESTURE_MS = { flick: FLICK_MS, ride: RIDE_MS };

const VETO_REASON = 'measured by the #9774 harness on the real graph';
const VETOES_NOT_APPLIED = ['click-ratio', 'runaway-tail', 'added-latency', 'dry-bit-exact', 'cpu'].map((veto) => ({ veto, reason: VETO_REASON }));
const PROXY_METHOD = {
	octave_span_at_gesture_end: '|log2(cornerHz at gesture end / cornerHz at gesture start)| from the corner trace the renderer returned for the clip',
	octave_span_fraction: 'octave_span_at_gesture_end over the full knob travel (HP 30 to 6300 Hz, LP 20000 to 130 Hz)',
	residual_hz_db: 'HP only: Goertzel level of a 100/160/250 Hz tone probe through the same gesture over [gesture end, +100 ms], output minus input, floor -120 dB',
	corner_peak_db: 'static noise probe at knob 0.75 HP / 0.25 LP, last 1 s: max output/input ratio over [fc/2, 2fc] in 1/12-octave steps minus the passband median',
	integrated_rms_change_db: '20 log10 of static-probe output RMS over the same probe at the shipped Q (LFO pairs: the shipped sine down LFO), last 1 s',
	lfo_pairs: 'LFO pairs hold the knob at the dry detent for the clip and the probes; the corner trace is the LFO lowpass'
};

export class VetoError extends Error {
	/** @param {{veto:string,pair:string,side:string,candidate:string,direction:string,gesture:string,fixture:string,render:string,index:number,measured:number,limit:number|string}} f */
	constructor(f) {
		const m = typeof f.measured === 'number' && Number.isFinite(f.measured) ? f.measured.toFixed(4) : String(f.measured);
		super(`${f.veto} veto: ${f.pair} ${f.side} (${f.candidate}, ${f.direction} ${f.gesture}, ${f.fixture}) ${f.render} sample ${f.index} measured ${m} > ${f.limit}`);
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
		throw new UnavailableError(`UNAVAILABLE: filter calibration renders only through the real web graph; missing ${names}`, missing);
	}
	throw new UnavailableError('UNAVAILABLE: the #9774 harness, #9826 and #9828 are present but this script has no adapter to them yet; wire the #9774 render entry on buildDeckChannelGraph with the resonance and glide params, and on buildBeatFxFilterKernel with lfo_shape and lfo_direction', []);
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
const _round = (x) => Math.round(100 * x) / 100 + 0; // + 0 turns -0 into 0
function _rms(channels, from = 0) {
	let sum = 0;
	let n = 0;
	for (const ch of channels) for (let i = from; i < ch.length; i++) { sum += ch[i] * ch[i]; n++; }
	return Math.sqrt(sum / n);
}
const _mid = (channels) => Float64Array.from(channels[0], (x, i) => (x + channels[1][i]) / 2);
/** Goertzel level in dB of frequency f over samples [from, to) of a mono buffer. */
function _goertzelDb(x, f, sr, from, to) {
	const w = (2 * Math.PI * f) / sr;
	const coeff = 2 * Math.cos(w);
	let s1 = 0;
	let s2 = 0;
	for (let i = from; i < to; i++) {
		const s0 = x[i] + coeff * s1 - s2;
		s2 = s1;
		s1 = s0;
	}
	const power = s1 * s1 + s2 * s2 - coeff * s1 * s2;
	const amp = (2 * Math.sqrt(Math.max(power, 0))) / (to - from);
	return Math.max(DB_FLOOR, 20 * Math.log10(amp || 1e-300));
}
const _quantumAt = (frame, quantum, len) => Math.min(len - 1, Math.max(0, Math.floor(frame / quantum)));
const _median = (xs) => {
	const s = [...xs].sort((a, b) => a - b);
	return s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
};

/** Full-travel knob automation: hold the dry detent until GESTURE_START_S, ramp to the end stop, hold. */
export function gesturePoints(direction, gestureMs) {
	const start = GESTURE_START_S * 1000;
	const end = direction === 'hp' ? 1 : 0;
	return [{ t_ms: 0, value: 0.5 }, { t_ms: start, value: 0.5 }, { t_ms: start + gestureMs, value: end }];
}
const _held = (value) => [{ t_ms: 0, value }];

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
	if (Math.abs(s.ic1) < 1e-30) s.ic1 = 0;
	if (Math.abs(s.ic2) < 1e-30) s.ic2 = 0;
	const y = c.type === 'bandpass' ? c.k * v1 : x - c.k * v1 - v2;
	return Math.abs(y) < 1e-30 ? 0 : y;
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
function _vocalLed(n, sr, seed) {
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
function _normaliseFixture(name, channels) {
	const gain = _dbToLin(DRY_RMS_DBFS) / _rms(channels);
	const scaled = channels.map((ch) => Float32Array.from(ch, (x) => x * gain));
	let peak = 0;
	for (const ch of scaled) for (const x of ch) peak = Math.max(peak, Math.abs(x));
	const peakDb = _round(20 * Math.log10(peak));
	if (peakDb > FIXTURE_PEAK_MAX_DBFS) {
		throw new Error(`fixture ${name} peaks at ${peakDb} dBFS after RMS normalisation to ${DRY_RMS_DBFS} dBFS, limit ${FIXTURE_PEAK_MAX_DBFS}`);
	}
	return { name, channels: scaled, rms_dbfs: DRY_RMS_DBFS, peak_dbfs: peakDb };
}
function _buildFixtures(seconds, sr, seed) {
	const n = Math.round(seconds * sr);
	return [
		_normaliseFixture('four-on-the-floor', _drumBus(n, sr, 128, seed ^ 0xd2, { snare: true, sixteenthHats: true })),
		_normaliseFixture('vocal-led', _vocalLed(n, sr, seed))
	];
}
function _toneProbe(n, sr) {
	const amp = _dbToLin(PROBE_TONE_DBFS);
	const ch = Float32Array.from({ length: n }, (_, i) => RESIDUAL_HZ.reduce((acc, f) => acc + amp * Math.sin((2 * Math.PI * f * i) / sr), 0));
	return [ch, Float32Array.from(ch)];
}
function _noiseProbe(n, seed) {
	const next = _rng(seed ^ 0x9829);
	const raw = Float64Array.from({ length: n }, () => next() * 2 - 1);
	const gain = _dbToLin(DRY_RMS_DBFS) / _rms([raw]);
	const ch = Float32Array.from(raw, (x) => x * gain);
	return [ch, Float32Array.from(ch)];
}

//----------------------------------------------------------------------------- render, veto, measure
function _checkResult(res, n, renderer, label) {
	const bad = (field) => { throw new Error(`renderer ${renderer.id} returned a malformed result for ${label}: ${field}`); };
	if (!Array.isArray(res?.channels) || res.channels.length !== 2) bad('channels must be [L, R]');
	for (const ch of res.channels) if (!ch || ch.length !== n) bad(`channels must have ${n} frames`);
	if (!Number.isInteger(res.quantum) || res.quantum < 1) bad('quantum must be a positive integer');
	if (!res.cornerHz || res.cornerHz.length < Math.ceil(n / res.quantum)) bad('cornerHz must cover the clip');
	for (const f of res.cornerHz) if (!Number.isFinite(f) || f <= 0) bad('cornerHz must be finite and positive');
}
function _checkFinite(channels, ctx) {
	for (const ch of channels) {
		for (let i = 0; i < ch.length; i++) if (!Number.isFinite(ch[i])) throw new VetoError({ ...ctx, veto: 'finite', index: i, measured: ch[i], limit: 'finite' });
	}
}
function _checkPeak(channels, ctx) {
	for (const ch of channels) {
		for (let i = 0; i < ch.length; i++) if (Math.abs(ch[i]) > 1) throw new VetoError({ ...ctx, veto: 'peak', index: i, measured: Math.abs(ch[i]), limit: '1.0' });
	}
}

function _measureProxies({ direction, gestureMs, sr, clip, tone, toneIn, probe, probeIn, reference }) {
	const startFrame = Math.round(GESTURE_START_S * sr);
	const endFrame = Math.round((GESTURE_START_S + gestureMs / 1000) * sr);
	const c = clip.cornerHz;
	const span = Math.abs(Math.log2(c[_quantumAt(endFrame, clip.quantum, c.length)] / c[_quantumAt(startFrame, clip.quantum, c.length)]));
	const travel = direction === 'hp' ? Math.log2(HP_CEILING_HZ / HP_FLOOR_HZ) : Math.log2(LP_CEILING_HZ / LP_FLOOR_HZ);
	const proxies = { octave_span_at_gesture_end: _round(span), octave_span_fraction: _round(span / travel) };

	for (const f of RESIDUAL_HZ) proxies[`residual_${f}_hz_db`] = null;
	if (direction === 'hp') {
		const to = Math.min(toneIn[0].length, endFrame + Math.round((RESIDUAL_WINDOW_MS / 1000) * sr));
		const [out, inp] = [_mid(tone.channels), _mid(toneIn)];
		for (const f of RESIDUAL_HZ) proxies[`residual_${f}_hz_db`] = _round(_goertzelDb(out, f, sr, endFrame, to) - _goertzelDb(inp, f, sr, endFrame, to));
	}

	const n = probeIn[0].length;
	const from = Math.max(0, n - Math.round(ANALYSIS_S * sr));
	const fc = probe.cornerHz[probe.cornerHz.length - 1];
	const [out, inp] = [_mid(probe.channels), _mid(probeIn)];
	const clampHz = (f) => Math.min(0.45 * sr, Math.max(20, f));
	const ratios = (lo, hi) => {
		const r = [];
		for (let f = clampHz(lo); f <= clampHz(hi) * 1.0001; f *= 2 ** (1 / 12)) r.push(_goertzelDb(out, f, sr, from, n) - _goertzelDb(inp, f, sr, from, n));
		return r.length ? r : [0];
	};
	const pass = direction === 'hp' ? ratios(2 * fc, 4 * fc) : ratios(fc / 4, fc / 2);
	proxies.corner_peak_db = _round(Math.max(...ratios(fc / 2, 2 * fc)) - _median(pass));
	proxies.integrated_rms_change_db = _round(20 * Math.log10(_rms(probe.channels, from) / _rms(reference.channels, from)));
	return proxies;
}

//----------------------------------------------------------------------------- manifest
const _labels = Object.keys(CANDIDATES);
/** @param {any} manifest */
export function assertPairsManifestShape(manifest) {
	// When #9776 lands tools/fx-ab/manifest.schema.json, validate against that file here and keep
	// these checks only for the meta block.
	const fail = (field) => { throw new Error(`pairs manifest shape: ${field}`); };
	if (typeof manifest?.family !== 'string' || !manifest.family) fail('family must be a non-empty string');
	if (!Number.isInteger(manifest.round) || manifest.round < 1) fail('round must be an integer >= 1');
	if (!Number.isInteger(manifest.seed) || manifest.seed < 0 || manifest.seed > 0xffffffff) fail('seed must be a uint32');
	if (typeof manifest.renderer?.id !== 'string' || !manifest.renderer.id || typeof manifest.renderer.real_path !== 'boolean') fail('renderer must be {id: non-empty string, real_path: boolean}');
	if (!Array.isArray(manifest.hidden_fields) || !manifest.hidden_fields.includes('meta')) fail('hidden_fields must include meta');
	if (!Array.isArray(manifest.pairs) || manifest.pairs.length < 1 || manifest.pairs.length > MAX_PAIRS) fail(`pairs must hold 1 to ${MAX_PAIRS} pairs`);
	const fixtures = new Set((manifest.fixtures ?? []).map((f) => f.name));
	const axes = Object.fromEntries((manifest.axes ?? []).map((a) => [a.axis, a.values]));
	const ids = new Set();
	for (const p of manifest.pairs) {
		if (typeof p.id !== 'string' || !p.id || ids.has(p.id)) fail(`pair id ${p.id} must be a unique string`);
		ids.add(p.id);
		for (const side of ['a', 'b']) if (typeof p[side] !== 'string' || !p[side].startsWith('clips/') || !p[side].endsWith('.wav')) fail(`pair ${p.id} ${side} must be a clips/*.wav path`);
		if (p.a === p.b) fail(`pair ${p.id} a/b must differ`);
		if (typeof p.question !== 'string' || !p.question) fail(`pair ${p.id} question`);
		const visible = `${p.id} ${p.a} ${p.b} ${p.question}`.toLowerCase();
		for (const label of _labels) if (visible.includes(label)) fail(`pair ${p.id} id/a/b/question leaks candidate label ${label}`);
		const m = p.meta;
		if (!m || !fixtures.has(m.fixture)) fail(`pair ${p.id} meta.fixture`);
		const values = axes[m.axis];
		if (!values || !values.includes(m.a_candidate) || !values.includes(m.b_candidate) || m.a_candidate === m.b_candidate) fail(`pair ${p.id} meta.axis/a_candidate/b_candidate`);
		for (const side of ['a', 'b']) {
			const px = m.proxies?.[side];
			if (!px) fail(`pair ${p.id} meta.proxies.${side}`);
			for (const k of ['octave_span_at_gesture_end', 'octave_span_fraction', 'corner_peak_db', 'integrated_rms_change_db']) {
				if (!Number.isFinite(px[k])) fail(`pair ${p.id} meta.proxies.${side}.${k}`);
			}
			for (const f of RESIDUAL_HZ) {
				const k = `residual_${f}_hz_db`;
				if (m.direction === 'hp' ? !Number.isFinite(px[k]) : px[k] !== null) fail(`pair ${p.id} meta.proxies.${side}.${k}`);
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

//----------------------------------------------------------------------------- export
const _lfoParams = (r) => ('lfo_shape' in r ? { lfo_shape: r.lfo_shape, lfo_direction: r.lfo_direction, lfo_beats: r.lfo_beats, lfo_bpm: r.lfo_bpm } : {});
const _params = (r) => ({ glide_domain: r.glide_domain, glide_ms: r.glide_ms, resonance_q_linear: r.resonance_q_linear, slope_db_oct: r.slope_db_oct, ..._lfoParams(r) });
function _request(label, winningGlide, lfoBpm) {
	const c = CANDIDATES[label];
	const lfo = lfoBpm === null ? {} : { ...LFO_SHIPPED, lfo_bpm: lfoBpm };
	return { ...SHIPPED, ...lfo, ...(c.winning ? winningGlide : {}), ...c.set };
}

/**
 * @param {{outDir:string, renderer:Renderer, allowTestDouble?:boolean, seed?:number, seconds?:number, sampleRate?:number, winningGlide?:{glide_domain:'hz'|'octave',glide_ms:number}, signal?:AbortSignal}} opts  abort signal = the caller replaced this export
 * @returns {Promise<{manifest:any, outDir:string}>}
 */
export async function exportFilterRatingPairs({ outDir, renderer, allowTestDouble = false, seed = DEFAULT_SEED, seconds = CLIP_SECONDS, sampleRate = SAMPLE_RATE, winningGlide = WINNING_GLIDE, signal }) {
	if (typeof renderer?.id !== 'string' || typeof renderer.realPath !== 'boolean' || typeof renderer.render !== 'function') throw new Error('renderer {id, realPath, render} is required');
	if (renderer.realPath !== true && !allowTestDouble) throw new Error(`renderer ${renderer.id} is not a real-path renderer; a calibration export needs the #9774 harness (pass allowTestDouble only from tests)`);
	if (!outDir) throw new Error('--out <dir> is required');
	if (signal !== undefined && !(signal instanceof AbortSignal)) throw new Error('signal must be an AbortSignal');
	if (existsSync(outDir) && readdirSync(outDir).length > 0) throw new Error(`--out ${outDir} is not empty; refusing to overwrite an earlier rating round`);
	const minSeconds = GESTURE_START_S + RIDE_MS / 1000 + 0.5;
	if (!(seconds >= minSeconds)) throw new Error(`--seconds ${seconds} is shorter than ${minSeconds} s (gesture start, 2 s ride and settle)`);
	if (!['hz', 'octave'].includes(winningGlide?.glide_domain) || !(winningGlide.glide_ms > 0)) throw new Error('winningGlide must be {glide_domain: hz|octave, glide_ms > 0}');
	seed >>>= 0;
	const sr = sampleRate;
	const fx = _buildFixtures(seconds, sr, seed);
	const n = fx[0].channels[0].length;
	const toneIn = _toneProbe(n, sr);
	const noiseIn = _noiseProbe(n, seed);

	// One seeded stream: a side swap per pair, then the pair order.
	const next = _rng(seed);
	const canonical = PAIRS.map((p) => ({ ...p, swap: next() < 0.5 }));
	for (let i = canonical.length - 1; i > 0; i--) {
		const j = Math.floor(next() * (i + 1));
		[canonical[i], canonical[j]] = [canonical[j], canonical[i]];
	}

	// Render, veto and measure everything in memory before writing anything.
	const references = new Map();
	const render = async (req, input, automation, label) => {
		const res = await renderer.render({ sampleRate: sr, input, automation, ...req });
		if (signal?.aborted) throw new Error(`renderer ${renderer.id} resolved ${label} after the export was replaced; stale result discarded`);
		_checkResult(res, n, renderer, label);
		return res;
	};
	const clips = [];
	const pairs = [];
	for (const [idx, c] of canonical.entries()) {
		const id = `p${String(idx + 1).padStart(2, '0')}`;
		const gestureMs = GESTURE_MS[c.gesture];
		const fixture = fx.find((f) => f.name === c.fixture);
		const [aCand, bCand] = c.swap ? [c.b, c.a] : [c.a, c.b];
		const isLfo = LFO_AXES.includes(c.axis);
		const sides = {};
		for (const [side, candidate] of [['a', aCand], ['b', bCand]]) {
			const req = { direction: c.direction, ..._request(candidate, winningGlide, isLfo ? FIXTURE_BPM[c.fixture] : null) };
			const ctx = { pair: c.key, side, candidate, direction: c.direction, gesture: c.gesture, fixture: c.fixture };
			const auto = isLfo ? _held(0.5) : gesturePoints(c.direction, gestureMs);
			const clip = await render(req, fixture.channels, auto, `${c.key} ${side} clip`);
			_checkFinite(clip.channels, { ...ctx, render: 'clip' });
			_checkPeak(clip.channels, { ...ctx, render: 'clip' });
			let tone = null;
			if (c.direction === 'hp') {
				tone = await render(req, toneIn, auto, `${c.key} ${side} tone probe`);
				_checkFinite(tone.channels, { ...ctx, render: 'tone probe' });
			}
			const held = isLfo ? _held(0.5) : _held(STATIC_PROBE_KNOB[c.direction]);
			const probe = await render(req, noiseIn, held, `${c.key} ${side} noise probe`);
			_checkFinite(probe.channels, { ...ctx, render: 'noise probe' });
			const refReq = { ...req, resonance_q_linear: SHIPPED.resonance_q_linear, ...(isLfo ? LFO_SHIPPED : {}) };
			const refKey = JSON.stringify(refReq);
			if (!references.has(refKey)) {
				const ref = await render(refReq, noiseIn, held, `${c.key} ${side} reference probe`);
				_checkFinite(ref.channels, { ...ctx, render: 'reference probe' });
				references.set(refKey, ref);
			}
			const proxies = _measureProxies({ direction: c.direction, gestureMs, sr, clip, tone, toneIn, probe, probeIn: noiseIn, reference: references.get(refKey) });
			sides[side] = { candidate, request: _params(req), proxies, path: `clips/${id}-${side}.wav` };
			clips.push([sides[side].path, clip.channels]);
		}
		pairs.push({
			id, a: sides.a.path, b: sides.b.path, question: isLfo ? LFO_QUESTION : QUESTION,
			meta: {
				key: c.key, axis: c.axis, fixture: c.fixture, direction: c.direction, gesture_ms: gestureMs,
				a_candidate: aCand, b_candidate: bCand, a_request: sides.a.request, b_request: sides.b.request,
				proxies: { a: sides.a.proxies, b: sides.b.proxies }
			}
		});
	}

	const manifest = {
		family: FAMILY, round: ROUND, seed,
		generated_by: 'scripts/export-filter-rating-pairs.mjs', renderer: { id: renderer.id, real_path: renderer.realPath },
		sample_rate: sr, clip_seconds: seconds,
		fixtures: fx.map(({ name, rms_dbfs, peak_dbfs }) => ({ name, rms_dbfs, peak_dbfs })),
		axes: AXES,
		baseline: { shipped: SHIPPED, winning_glide: { ...winningGlide }, winning_glide_source: winningGlide === WINNING_GLIDE ? 'CFG' : 'cli', lfo_shipped: LFO_SHIPPED },
		proxy_method: PROXY_METHOD,
		vetoes_applied: ['finite', 'peak'],
		vetoes_not_applied: VETOES_NOT_APPLIED,
		hidden_fields: ['meta'],
		pairs
	};
	assertPairsManifestShape(manifest);

	mkdirSync(join(outDir, 'clips'), { recursive: true });
	for (const [rel, channels] of clips) writeFileSync(join(outDir, rel), _encodeWav(channels, sr));
	writeFileSync(join(outDir, 'pairs.json'), `${JSON.stringify(manifest, null, 2)}\n`);
	return { manifest, outDir };
}

//----------------------------------------------------------------------------- CLI
export function _parseArgs(argv) {
	const opts = {};
	for (let i = 0; i < argv.length; i++) {
		const flag = argv[i];
		const val = argv[++i];
		if (val === undefined) throw new Error(`${flag} needs a value`);
		if (flag === '--out') opts.outDir = val;
		else if (flag === '--seed') opts.seed = Number(val);
		else if (flag === '--seconds') opts.seconds = Number(val);
		else if (flag === '--sample-rate') opts.sampleRate = Number(val);
		else if (flag === '--winning-glide') {
			const [domain, ms] = val.split(':');
			if (!['hz', 'octave'].includes(domain) || ![5, 10, 20].includes(Number(ms))) throw new Error(`--winning-glide expects hz|octave:<5|10|20>, got ${val}`);
			opts.winningGlide = { glide_domain: domain, glide_ms: Number(ms) };
		} else throw new Error(`unknown argument ${flag}`);
	}
	if ('seed' in opts && !(Number.isInteger(opts.seed) && opts.seed >= 0 && opts.seed <= 0xffffffff)) throw new Error('invalid --seed');
	for (const k of ['seconds', 'sampleRate']) if (k in opts && !(opts[k] > 0)) throw new Error(`invalid --${k}`);
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
		if (!(err instanceof UnavailableError)) {
			console.error(`[ERROR] ${err.message}`);
			return 1;
		}
		console.error(`[UNAVAILABLE] ${err.message}`);
		return EXIT_UNAVAILABLE;
	}
	try {
		const { manifest: m } = await exportFilterRatingPairs({ ...opts, renderer });
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
