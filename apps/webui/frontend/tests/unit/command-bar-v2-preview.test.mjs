// requirement: CMDK-07
// requirement: CMDK-08
// requirement: CMDK-09
// requirement: CMDK-10
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, mock, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Cmd K slice 2 (#6090): the jump grid and its snap stops, the in-phase start,
 * the cue preview controller driving the dispatcher's preview commands, the
 * deck picker's rules and the two flags.
 *
 * The track is invented: 64 bars of 4/4 at 120 BPM (2 s bars) whose first
 * downbeat is at 0.5 s. Sections and vocals are placed on known bars, a little
 * off the downbeat the way analysis lands them.
 *
 * Regression lines:
 * - if → from bar 1 sails past a drop inside the first 4 bars then broken
 * - if a build-up or an intro becomes a snap stop then broken
 * - if every gap between the words of one verse becomes a stop then broken
 * - if a vocal entry after a bar of no vocals is not a stop then broken
 * - if → then ← does not land back on the bar it started from then broken
 * - if a landing does not start the master's fraction into its bar then broken
 * - if a held → sends a seek per press instead of the latest one then broken
 * - if a stop is sent while a start is still decoding then broken
 * - if the picker lets the live master be picked then broken
 * - if replacing a playing deck loads without asking then broken
 * - if a highlight rests fewer than 90 ms then requestPreviewAssets fires then broken
 * - if the highlight moves before 90 ms then the left-behind row was fetched anyway then broken
 * - if the bar closes before 90 ms then a fetch still landed then broken
 */

let grid;
let beatgridFallback;
let cuePreview;
let picker;
let flag;

before(async () => {
	grid = await loadTypeScriptModule('src/lib/rb/cmdk-preview-grid.ts');
	beatgridFallback = await loadTypeScriptModule('src/lib/rb/beatgrid-fallback.ts');
	cuePreview = await loadTypeScriptModule('src/lib/rb/cmdk-cue-preview.svelte.ts');
	picker = await loadTypeScriptModule('src/lib/rb/cmdk-deck-picker.ts');
	flag = await loadTypeScriptModule('src/lib/rb/cmdk-flag.ts');
});

function matchBrace(s, openBraceIdx) {
	let depth = 0;
	for (let i = openBraceIdx; i < s.length; i++) {
		if (s[i] === '{') depth++;
		else if (s[i] === '}') {
			depth -= 1;
			if (depth === 0) return i;
		}
	}
	throw new Error('unbalanced braces');
}

function readCommandBarSource() {
	return readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
}

function extractPreviewDwellEffectArrow(bar) {
	const anchor = '// Ask for the overview only after a rest on the row';
	const anchorIdx = bar.indexOf(anchor);
	assert.notEqual(anchorIdx, -1, `${anchor} not found`);
	const header = '$effect(() => {';
	const headerStart = bar.indexOf(header, anchorIdx);
	assert.notEqual(headerStart, -1, `${header} not found after dwell anchor`);
	const braceStart = headerStart + header.length - 1;
	const braceEnd = matchBrace(bar, braceStart);
	return bar.slice(headerStart + '$effect('.length, braceEnd + 1);
}

function parsePreviewDwellMs(bar) {
	const m = /const PREVIEW_DWELL_MS = (\d+);/.exec(bar);
	assert.ok(m, 'PREVIEW_DWELL_MS not found');
	return Number(m[1]);
}

function createPreviewDwellHarness(bar) {
	const PREVIEW_DWELL_MS = parsePreviewDwellMs(bar);
	const effectArrow = extractPreviewDwellEffectArrow(bar);
	// eslint-disable-next-line no-new-func -- runs shipped CommandBarV2 dwell effect text
	const harness = new Function(
		`let previewId = null;
let dwelled = null;
let dwellAt = 0;
let dwellId = null;
let firstPaintMs = null;
const watchPreviewCalls = [];
const requestPreviewAssetsCalls = [];
const watchPreview = (stable_id) => watchPreviewCalls.push(stable_id);
const requestPreviewAssets = (stable_id) => requestPreviewAssetsCalls.push(stable_id);
let perfNow = 1000;
const performance = { now: () => perfNow };
const PREVIEW_DWELL_MS = ${PREVIEW_DWELL_MS};
const __effect__ = ${effectArrow};
return {
	setPreviewId(id) { previewId = id; },
	run() { return __effect__(); },
	getState() { return { dwelled, dwellAt, dwellId, firstPaintMs, previewId }; },
	calls: { watchPreview: watchPreviewCalls, requestPreviewAssets: requestPreviewAssetsCalls },
	PREVIEW_DWELL_MS: ${PREVIEW_DWELL_MS}
};`
	);
	return harness();
}

const FIRST = 0.5;
const BAR = 2;
const BARS = 64;
/** Seconds of bar `n` (0-based) plus `off` seconds. */
const at = (n, off = 0) => FIRST + n * BAR + off;

function beats() {
	const out = [];
	for (let bar = 0; bar < BARS; bar++) {
		for (let b = 0; b < 4; b++) out.push({ n: b + 1, bpm: 120, t: at(bar) + b * 0.5 });
	}
	return out;
}

function previewAnlz(previewBeats) {
	const emptyBands = { length: 0, low: [], mid: [], high: [] };
	return {
		stable_id: 'abc123',
		points: 38400,
		waveform: { kind: 'mono', preview: emptyBands, detail: emptyBands },
		beatgrid: { beat_count: previewBeats.length, beats: previewBeats },
		cues: [],
		phrases: [],
		local_waveform: { status: 'decoded' }
	};
}

function previewFallback(fallbackBeats) {
	return {
		stable_id: 'abc123',
		source: 'apps.analysis',
		backend: 'librosa',
		backend_version: 'test',
		bpm: 128,
		bpm_confidence: 0.9,
		anlz_available: false,
		beatgrid: {
			source: 'own',
			status: 'ok',
			beat_count: fallbackBeats.length,
			beats: fallbackBeats
		}
	};
}

const SECTIONS = [
	{ start_s: at(0), label: 'Intro', family: 'edge' },
	{ start_s: at(3, 0.08), label: 'Buildup', family: 'rise' },
	{ start_s: at(6, -0.1), label: 'Drop', family: 'drop' },
	{ start_s: at(22, 0.05), label: 'Breakdown', family: 'calm' },
	{ start_s: at(30), label: 'Drop', family: 'drop' },
	{ start_s: at(56), label: 'Outro', family: 'edge' }
];
const VOCALS = [
	// One verse: words with gaps shorter than a bar between them.
	{ start_s: at(40, 0.1), end_s: at(41) },
	{ start_s: at(41, 0.6), end_s: at(42, 1.2) },
	{ start_s: at(43, 0.4), end_s: at(44) },
	// A new entry after two bars of no vocals.
	{ start_s: at(46, 0.05), end_s: at(48) }
];

function track() {
	const downbeats = grid.downbeatTimes(beats());
	return { downbeats, stops: grid.snapStops(downbeats, SECTIONS, VOCALS) };
}

test('downbeats are the beats numbered 1, in time order', () => {
	const shuffled = [...beats()].reverse();
	const db = grid.downbeatTimes(shuffled);
	assert.equal(db.length, BARS);
	assert.equal(db[0], FIRST);
	assert.equal(db[1], at(1));
	assert.ok(db.every((t, i) => i === 0 || t > db[i - 1]));
});

test('the preview uses /beatgrid-fallback downbeats when /anlz has no grid', () => {
	const fallbackBeats = [
		{ n: 1, bpm: 128, t: 0.25 },
		{ n: 2, bpm: 128, t: 0.71875 },
		{ n: 3, bpm: 128, t: 1.1875 },
		{ n: 4, bpm: 128, t: 1.65625 },
		{ n: 1, bpm: 128, t: 2.125 }
	];
	const anlz = previewAnlz([]);
	const gate = {
		anlzErrorCode: null,
		anlz,
		vendor: null,
		effectiveSource: 'rekordbox'
	};
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), true);
	const painted = beatgridFallback.resolvePaintAnlz(anlz, previewFallback(fallbackBeats));
	assert.deepEqual(grid.downbeatTimes(painted.beatgrid.beats), [0.25, 2.125]);
});

test('the preview ignores /beatgrid-fallback when /anlz has a real grid', () => {
	const anlzBeats = [
		{ n: 1, bpm: 124, t: 0.5 },
		{ n: 2, bpm: 124, t: 0.983 },
		{ n: 3, bpm: 124, t: 1.467 },
		{ n: 4, bpm: 124, t: 1.951 },
		{ n: 1, bpm: 124, t: 2.435 }
	];
	const fallbackBeats = [
		{ n: 1, bpm: 128, t: 7.25 },
		{ n: 2, bpm: 128, t: 7.71875 },
		{ n: 3, bpm: 128, t: 8.1875 },
		{ n: 4, bpm: 128, t: 8.65625 },
		{ n: 1, bpm: 128, t: 9.125 }
	];
	const anlz = previewAnlz(anlzBeats);
	const gate = {
		anlzErrorCode: null,
		anlz,
		vendor: null,
		effectiveSource: 'rekordbox'
	};
	assert.equal(beatgridFallback.shouldUseBeatgridFallback(gate), false);
	const painted = beatgridFallback.resolvePaintAnlz(anlz, previewFallback(fallbackBeats));
	assert.deepEqual(grid.downbeatTimes(painted.beatgrid.beats), [0.5, 2.435]);
	assert.ok(!painted.beatgrid.beats.some((beat) => beat.t === 7.25));
});

test('snap stops are the drops, breaks and vocal entries, on their nearest bar', () => {
	const { stops } = track();
	assert.deepEqual(
		stops.map((s) => [s.bar, s.kind, s.label]),
		[
			[6, 'drop', 'Drop'],
			[22, 'break', 'Breakdown'],
			[30, 'drop', 'Drop'],
			[40, 'vocal', 'Vocal'],
			[46, 'vocal', 'Vocal']
		]
	);
	// Control: the build-up, intro and outro are real sections and still not stops.
	assert.ok(SECTIONS.some((s) => s.family === 'rise'));
	assert.ok(!stops.some((s) => [0, 3, 56].includes(s.bar)));
});

test('a section and a vocal on one bar keep the section name', () => {
	const db = grid.downbeatTimes(beats());
	const stops = grid.snapStops(
		db,
		[{ start_s: at(10), label: 'Drop', family: 'drop' }],
		[{ start_s: at(10, 0.2), end_s: at(12) }]
	);
	assert.deepEqual(stops, [{ bar: 10, kind: 'drop', label: 'Drop' }]);
});

test('a vocal less than a bar after the track starts is not an entry (Codex r4278768635)', () => {
	const db = grid.downbeatTimes(beats());
	assert.deepEqual(
		grid.snapStops(db, [], [{ start_s: at(0, 1.2), end_s: at(3) }]).length,
		0,
		'under a bar of silence before it, though nearest bar 2'
	);
	// Control: a first vocal after a full bar of silence is still a stop.
	assert.deepEqual(grid.snapStops(db, [], [{ start_s: at(4), end_s: at(6) }]), [
		{ bar: 4, kind: 'vocal', label: 'Vocal' }
	]);
});

test('no beat grid means no stops, and nearestBar refuses rather than guessing', () => {
	assert.deepEqual(grid.snapStops([], SECTIONS, VOCALS), []);
	assert.throws(() => grid.nearestBar([], 3), /no downbeats/);
});

test('→ from bar 1 stops at a drop inside the first 4 bars, then keeps the fixed grid', () => {
	const { downbeats, stops } = track();
	const g4 = grid.jumpGrid(
		downbeats.length,
		stops.map((s) => s.bar),
		grid.PREVIEW_STEP_BARS
	);
	const walk = [0];
	for (let i = 0; i < 4; i++) walk.push(grid.jumpTarget(g4, walk.at(-1), 1));
	// 4 is on the grid before the drop at 6; after the drop the grid is 8, not 10.
	assert.deepEqual(walk, [0, 4, 6, 8, 12]);
	// Shift: 8-bar steps, still stopping at the drop and the break.
	const g8 = grid.jumpGrid(
		downbeats.length,
		stops.map((s) => s.bar),
		grid.PREVIEW_BIG_STEP_BARS
	);
	const big = [0];
	for (let i = 0; i < 5; i++) big.push(grid.jumpTarget(g8, big.at(-1), 1));
	assert.deepEqual(big, [0, 6, 8, 16, 22, 24]);
});

test('the grid never runs past the last bar, and ends stay put', () => {
	const g = grid.jumpGrid(10, [3, 12, 0], 4);
	assert.deepEqual(g, [0, 3, 4, 8]);
	assert.equal(grid.jumpTarget(g, 8, 1), 8);
	assert.equal(grid.jumpTarget(g, 0, -1), 0);
	assert.deepEqual(grid.jumpGrid(8, [], 4), [0, 4], 'bar 8 of 8 does not exist');
	assert.deepEqual(grid.jumpGrid(0, [], 4), []);
	assert.throws(() => grid.jumpGrid(10, [], 0), /positive integer/);
});

test('→ then ← lands on the same bar, from anywhere on the grid', () => {
	const { downbeats, stops } = track();
	for (const step of [grid.PREVIEW_STEP_BARS, grid.PREVIEW_BIG_STEP_BARS]) {
		const g = grid.jumpGrid(
			downbeats.length,
			stops.map((s) => s.bar),
			step
		);
		for (const bar of g.slice(0, -1)) {
			assert.equal(
				grid.jumpTarget(g, grid.jumpTarget(g, bar, 1), -1),
				bar,
				`step ${step} from bar ${bar}`
			);
		}
	}
});

test('a landing starts the master phase into its bar', () => {
	const db = grid.downbeatTimes(beats());
	assert.equal(grid.barPhase(db, at(5, 0.5)), 0.25);
	assert.equal(grid.barPhase(db, 0.1), null, 'before the first downbeat');
	assert.equal(grid.barPhase(db, at(70)), null, 'past the last bar');
	// Inside the final bar the phase still reads, from the bar before's length (Sol r4278366227).
	assert.equal(grid.barPhase(db, at(BARS - 1, 1)), 0.5, 'inside the final bar');
	assert.equal(grid.barPhase(db, at(BARS - 1)), 0, 'on the final downbeat');
	assert.equal(grid.barPhase(db, at(BARS, 0.5)), null, "past the final bar's estimated end");
	assert.equal(grid.inPhaseStartSec(db, 6, 0.25), at(6, 0.5));
	assert.equal(grid.inPhaseStartSec(db, 6, 0), at(6));
	// The last bar has no next downbeat, so it borrows the bar before's length.
	assert.equal(grid.inPhaseStartSec(db, BARS - 1, 0.5), at(BARS - 1, 1));
	assert.throws(() => grid.inPhaseStartSec(db, BARS, 0), /outside the grid/);
});

test('latestOnly runs one at a time and keeps only the newest waiting ask', async () => {
	const ran = [];
	const gates = [];
	const run = grid.latestOnly(async (n) => {
		ran.push(n);
		await new Promise((resolve) => gates.push(resolve));
	});
	const p1 = run(1);
	const p2 = run(2);
	const p3 = run(3);
	assert.deepEqual(ran, [1]);
	gates.shift()();
	await p1;
	await new Promise((r) => setImmediate(r));
	assert.deepEqual(ran, [1, 3], 'the waiting 2 was replaced by 3');
	gates.shift()();
	await Promise.all([p2, p3]);
	// Control: once idle, the next ask runs at once.
	const p4 = run(4);
	assert.deepEqual(ran, [1, 3, 4]);
	gates.shift()();
	await p4;
});

test('latestOnly rejects the waiting asks when the run that replaced them fails (Sol r4279823893)', async () => {
	const gates = [];
	const run = grid.latestOnly(async (n) => {
		await new Promise((resolve) => gates.push(resolve));
		if (n === 3) throw new Error('no grid for bar 3');
	});
	const p1 = run(1);
	const p2 = run(2);
	const p3 = run(3);
	gates.shift()();
	await p1;
	await new Promise((r) => setImmediate(r));
	gates.shift()();
	// Both asks the failed run stood for see its failure, the replaced 2 included.
	await assert.rejects(p2, /no grid for bar 3/);
	await assert.rejects(p3, /no grid for bar 3/);
	// Control: a run that succeeds still resolves its waiters, and the queue keeps working.
	const p4 = run(4);
	const p5 = run(5);
	gates.shift()();
	await p4;
	await new Promise((r) => setImmediate(r));
	gates.shift()();
	await p5;
	// And a first ask's own failure rejects it directly.
	const p6 = run(3);
	gates.shift()();
	await assert.rejects(p6, /no grid for bar 3/);
});

/** The controller over a recording dispatcher; `decode` holds the next start open. */
function harness({ phase = 0.25 } = {}) {
	const sent = [];
	let sounding = null;
	let clock = 0;
	let hold = null;
	let refuse = false;
	/** Indexes into `sent` that are refused on their own, so one start can fail while the next lands. */
	const refused = new Set();
	const ports = {
		dispatch: async (command) => {
			sent.push(command);
			const index = sent.length - 1;
			if (command.type === 'preview_stop') {
				sounding = null;
				return true;
			}
			if (hold !== null) await hold.promise;
			// A refused start leaves whatever was sounding in place.
			if (refuse || refused.has(index)) return false;
			sounding = command.stable_id;
			return true;
		},
		masterPhase: () => phase,
		sounding: (id) => sounding === id,
		now: () => clock
	};
	const cue = cuePreview.createCuePreview(ports);
	return {
		cue,
		sent,
		tick: (ms) => (clock += ms),
		refuse: (on) => (refuse = on),
		refuseSend: (index) => refused.add(index),
		holdNext() {
			let release;
			const promise = new Promise((r) => (release = r));
			hold = { promise };
			return () => {
				hold = null;
				release();
			};
		}
	};
}

function cueTrack(id = 'a'.repeat(40)) {
	const { downbeats, stops } = track();
	return { stable_id: id, downbeats, stops, duration_s: at(BARS), bpm: 120, gridPending: false };
}

test('the first → starts at bar 1 in phase, then each press walks the grid through the dispatcher', async () => {
	const h = harness();
	const t = cueTrack();
	h.cue.setTrack(t);
	await h.cue.jump(1, false);
	assert.equal(h.sent.length, 1);
	assert.equal(h.sent[0].type, 'preview_cue');
	assert.equal(h.sent[0].stable_id, t.stable_id);
	assert.equal(h.sent[0].bpm, 120);
	assert.ok(Math.abs(h.sent[0].ratio - at(0, 0.5) / t.duration_s) < 1e-12, 'bar 1, a quarter in');
	assert.equal(h.cue.state.message, 'Bar 1');
	await h.cue.jump(1, false);
	await h.cue.jump(1, false);
	assert.equal(h.cue.state.anchor, 6);
	assert.equal(h.cue.state.message, 'Drop at bar 7');
	assert.ok(Math.abs(h.sent.at(-1).ratio - at(6, 0.5) / t.duration_s) < 1e-12);
	await h.cue.jump(-1, false);
	assert.equal(h.cue.state.anchor, 4);
	await h.cue.jump(1, false);
	assert.equal(h.cue.state.anchor, 6, '→ then ← then → is back on the drop');
});

test('a slow first start is restarted once from the cache, back in phase', async () => {
	const h = harness();
	h.cue.setTrack(cueTrack());
	const release = h.holdNext();
	const pending = h.cue.jump(1, false);
	h.tick(cuePreview.COLD_START_RESEEK_MS + 200);
	release();
	await pending;
	assert.deepEqual(
		h.sent.map((c) => c.type),
		['preview_cue', 'preview_cue']
	);
	// Control: a fast start is not restarted.
	const fast = harness();
	fast.cue.setTrack(cueTrack());
	await fast.cue.jump(1, false);
	assert.equal(fast.sent.length, 1);
	// A slow later jump on a sounding preview is not a cold decode, so it is sent once (Codex r4278768639).
	const warm = harness();
	warm.cue.setTrack(cueTrack());
	await warm.cue.jump(1, false);
	const hold = warm.holdNext();
	const later = warm.cue.jump(1, false);
	warm.tick(cuePreview.COLD_START_RESEEK_MS + 200);
	hold();
	await later;
	assert.equal(warm.sent.length, 2, 'the start and the jump, no reseek');
});

test('presses during a cold start move the anchor and the latest one is the seek that follows', async () => {
	const h = harness();
	h.cue.setTrack(cueTrack());
	const release = h.holdNext();
	const first = h.cue.jump(1, false);
	const second = h.cue.jump(1, false);
	const third = h.cue.jump(1, false);
	assert.equal(h.cue.state.anchor, 6, 'two presses after the start: 0, 4, 6');
	release();
	await Promise.all([first, second, third]);
	assert.equal(h.sent.length, 2, 'the start and one seek, not one per press');
	assert.ok(Math.abs(h.sent[1].ratio - at(6, 0.5) / cueTrack().duration_s) < 1e-12);
});

test('Esc stops a sounding preview; a stop during a decode waits for it instead of cancelling it', async () => {
	const h = harness();
	h.cue.setTrack(cueTrack());
	await h.cue.jump(1, false);
	assert.equal(h.cue.stop(), true);
	assert.equal(h.sent.at(-1).type, 'preview_stop');
	assert.equal(h.cue.stop(), false, 'nothing left to stop');

	const cold = harness();
	cold.cue.setTrack(cueTrack());
	const release = cold.holdNext();
	const pending = cold.cue.jump(1, false);
	// Esc during a cold decode still belongs to the preview, so it does not also clear or close the bar.
	assert.equal(cold.cue.stop(), true, 'a start on its way counts as sounding for Esc');
	assert.equal(cold.cue.stop(), false, 'the second Esc goes on to the bar');
	assert.deepEqual(
		cold.sent.map((c) => c.type),
		['preview_cue'],
		'no stop while the start decodes'
	);
	release();
	await pending;
	assert.deepEqual(
		cold.sent.map((c) => c.type),
		['preview_cue', 'preview_stop']
	);

	// The same while a jump's seek is under way over a preview that is sounding.
	const warm = harness();
	warm.cue.setTrack(cueTrack());
	await warm.cue.jump(1, false);
	const releaseSeek = warm.holdNext();
	const seeking = warm.cue.jump(1, false);
	warm.cue.stop();
	assert.deepEqual(
		warm.sent.map((c) => c.type),
		['preview_cue', 'preview_cue'],
		'no stop while the seek runs'
	);
	releaseSeek();
	await seeking;
	assert.equal(warm.sent.at(-1).type, 'preview_stop');
});

test('a refused later jump keeps owning the preview still sounding, so Esc stops it; a refused first start owns nothing (Codex r4279062913)', async () => {
	const h = harness();
	h.cue.setTrack(cueTrack());
	await h.cue.jump(1, false);
	h.refuse(true);
	await h.cue.jump(1, false);
	assert.equal(h.cue.state.started, true, "the old preview is still this bar's");
	assert.equal(h.cue.stop(), true, 'Esc belongs to the preview');
	assert.equal(h.sent.at(-1).type, 'preview_stop');

	// Control: a refused first start leaves nothing sounding and nothing to stop.
	const cold = harness();
	cold.cue.setTrack(cueTrack());
	cold.refuse(true);
	await cold.cue.jump(1, false);
	assert.equal(cold.cue.state.started, false);
	assert.equal(cold.cue.stop(), false, 'Esc goes on to the bar');
	assert.deepEqual(
		cold.sent.map((c) => c.type),
		['preview_cue']
	);
});

test('Esc consumes a press held for a beat grid, though nothing has started (Sol)', async () => {
	const h = harness();
	h.cue.setTrack({ ...cueTrack(), downbeats: [], stops: [], gridPending: true });
	await h.cue.jump(1, false);
	assert.equal(h.cue.state.started, false);
	assert.equal(h.cue.stop(), true, "the held press is the preview's to consume");
	assert.deepEqual(h.sent, [], 'nothing was sent for the held press');

	// Control: with nothing held and nothing sounding, Esc goes on to the bar.
	const idle = harness();
	idle.cue.setTrack(cueTrack());
	assert.equal(idle.cue.stop(), false);
});

test('a refused jump puts the anchor and its trail back on the bar still sounding and names the refusal (Sol, Codex)', async () => {
	const h = harness();
	h.cue.setTrack(cueTrack());
	await h.cue.jump(1, false);
	const sounding = { anchor: h.cue.state.anchor };
	h.refuse(true);
	await h.cue.jump(1, false);
	assert.equal(
		h.cue.state.anchor,
		sounding.anchor,
		'the anchor is the bar still sounding, not the refused one'
	);
	assert.equal(h.cue.state.message, cuePreview.PREVIEW_REFUSED);
	assert.equal(h.cue.state.started, true);
});

test('moving to another track stops the old preview and starts the new one at bar 1', async () => {
	const h = harness();
	h.cue.setTrack(cueTrack('a'.repeat(40)));
	await h.cue.jump(1, false);
	await h.cue.jump(1, false);
	h.cue.setTrack(cueTrack('b'.repeat(40)));
	assert.equal(h.sent.at(-1).type, 'preview_stop');
	assert.equal(h.cue.state.anchor, 0);
	await h.cue.jump(1, false);
	assert.equal(h.sent.at(-1).stable_id, 'b'.repeat(40));
	// Control: new data for the SAME track (the /anlz landing) does not stop it.
	const before = h.sent.length;
	h.cue.setTrack({ ...cueTrack('b'.repeat(40)), stops: [] });
	assert.equal(h.sent.length, before);
});

test('no beat grid or no length: a named reason, and nothing is dispatched', async () => {
	const h = harness();
	h.cue.setTrack({ ...cueTrack(), downbeats: [], stops: [] });
	await h.cue.jump(1, false);
	assert.match(h.cue.state.message, /beat grid/);
	h.cue.setTrack({ ...cueTrack('c'.repeat(40)), duration_s: null });
	await h.cue.jump(1, false);
	assert.match(h.cue.state.message, /length/);
	assert.equal(h.sent.length, 0);
});

const DECKS = [
	{
		id: 1,
		title: 'Paper Lantern',
		artist: 'Invented',
		loaded: true,
		playing: false,
		is_master: false
	},
	{
		id: 2,
		title: 'Glasshouse Tide',
		artist: 'Invented',
		loaded: true,
		playing: true,
		is_master: true
	},
	{ id: 3, title: 'Low Ferry', artist: 'Invented', loaded: true, playing: true, is_master: false },
	{ id: 4, title: null, artist: null, loaded: false, playing: false, is_master: false }
];

test('the picker lists the master first and never lands on it', () => {
	const order = picker.pickerOrder(DECKS);
	assert.deepEqual(
		order.map((d) => d.id),
		[2, 1, 3, 4]
	);
	assert.equal(picker.pickerStart(order, 2), 1, 'a target on the master opens on the next deck');
	assert.equal(picker.pickerStart(order, 4), 3);
	assert.equal(
		picker.pickerStep(order, 1, -1),
		1,
		'↑ from the first free deck stays, skipping the master'
	);
	assert.equal(picker.pickerStep(order, 1, 1), 2);
	assert.equal(picker.pickerStep(order, 3, 1), 3);
	// Control: with no master the first deck is reachable.
	const none = picker.pickerOrder(DECKS.map((d) => ({ ...d, is_master: false })));
	assert.equal(picker.pickerStep(none, 1, -1), 0);
});

test('picking: the master is refused, a playing deck asks first, the rest load', () => {
	const [master, paused, playing, empty] = picker.pickerOrder(DECKS);
	assert.equal(picker.pickerAction(master, true), 'refuse', 'even confirmed');
	assert.equal(picker.pickerAction(playing, false), 'ask');
	assert.equal(picker.pickerAction(playing, true), 'load');
	assert.equal(picker.pickerAction(paused, false), 'load');
	assert.equal(picker.pickerAction(empty, false), 'load');
});

test('WASD are arrows in the picker, but not with a modifier', () => {
	const key = (code, key, mods = {}) => ({
		code,
		key,
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		...mods
	});
	assert.equal(picker.pickerArrow(key('KeyW', 'w')), 'ArrowUp');
	assert.equal(picker.pickerArrow(key('KeyS', 'S', {})), 'ArrowDown');
	assert.equal(picker.pickerArrow(key('KeyA', 'a')), 'ArrowLeft');
	assert.equal(picker.pickerArrow(key('KeyD', 'd')), 'ArrowRight');
	assert.equal(picker.pickerArrow(key('ArrowUp', 'ArrowUp', { altKey: true })), 'ArrowUp');
	assert.equal(picker.pickerArrow(key('KeyW', 'w', { metaKey: true })), null);
	assert.equal(picker.pickerArrow(key('KeyQ', 'q')), null);
});

test('the preview flags: off unless declared on, inline only on top of the preview flag', () => {
	const f = (pairs) => pairs.map(([flag_id, enabled]) => ({ flag_id, enabled }));
	assert.equal(flag.cmdkPreviewModeIn([]), 'off');
	assert.equal(flag.cmdkPreviewModeIn(f([['cmdk.v2.preview_inline', true]])), 'off');
	assert.equal(
		flag.cmdkPreviewModeIn(
			f([
				['cmdk.v2.preview', false],
				['cmdk.v2.preview_inline', true]
			])
		),
		'off'
	);
	assert.equal(flag.cmdkPreviewModeIn(f([['cmdk.v2.preview', true]])), 'above');
	assert.equal(
		flag.cmdkPreviewModeIn(
			f([
				['cmdk.v2.preview', true],
				['cmdk.v2.preview_inline', true]
			])
		),
		'inline'
	);
});

test('a click on a picker row leaves the picker keys working from the bar wrapper', () => {
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	const wrap = bar.slice(bar.indexOf('function onWrapKeydown'), bar.indexOf('function fmtBpm'));
	assert.match(
		wrap,
		/if \(previewMode !== 'off' && picker !== null && pickerKey\(event\)\) \{\s*event\.preventDefault\(\);/
	);
	assert.ok(
		wrap.indexOf('event.stopPropagation()') < wrap.indexOf('pickerKey(event)'),
		'the page never sees the key'
	);
});

test('the picker holds its deck, not its row, so a new master never moves Enter to another deck', () => {
	// The order moves when the master changes: the deck at row 1 is a different deck after.
	const before = picker.pickerOrder(DECKS);
	const after = picker.pickerOrder(DECKS.map((d) => ({ ...d, is_master: d.id === 4 })));
	assert.equal(before[2].id, 3);
	assert.equal(after[2].id, 2, 'precondition: row 2 is now another deck');
	assert.equal(
		after.findIndex((d) => d.id === 3),
		3,
		'deck 3 is still found by its id'
	);
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		bar,
		/let picker = \$state<\{ row: CmdkRow; vocalsOnly: boolean; sel: DeckId; guard: ReplaceGuard \| null[;} ]/
	);
	assert.match(
		bar,
		/const pickerSel = \$derived\(picker === null \? -1 : pickerDecks\.findIndex\(\(deck\) => deck\.id === picker\?\.sel\)\);/
	);
	const keys = bar.slice(
		bar.indexOf('function pickerKey'),
		bar.indexOf('function pickerKey') + 2000
	);
	assert.match(keys, /pickerLoad\(pickerSel, false\)/);
	assert.match(bar, /selected=\{pickerSel\}/);
});

test('a strip still being analyzed is asked for again, a bounded number of times, without flicker', () => {
	const data = readFileSync(
		new URL('../../src/lib/rb/cmdk-preview-data.svelte.ts', import.meta.url),
		'utf8'
	);
	const fill = readFileSync(
		new URL('../../src/lib/rb/row-assets-fill.ts', import.meta.url),
		'utf8'
	);
	assert.match(fill, /pending: pendingList\(answer\.pending\)\.includes\(stable_id\)/);
	// A missing or malformed pending list is contract drift: it throws rather than reading as done (Sol P1, #6687).
	assert.match(fill, /throw new Error\('row assets answered without a pending list/);
	assert.match(data, /if \(!asset\.pending \|\| tries > PENDING_TRIES\)/);
	// The retry refetches into the held entry, so the strip never drops back to loading.
	assert.match(
		data,
		/if \(_assets\.get\(stable_id\) !== entry\) return;\s*if \(_watched === stable_id\) _fetchInto\(stable_id\);/
	);
	assert.doesNotMatch(data.slice(data.indexOf('function _fetchInto')), /status: 'loading'/);
});

test('a strip still pending when its retries run out is asked for again on the next visit (Codex r4275809110)', () => {
	const data = readFileSync(
		new URL('../../src/lib/rb/cmdk-preview-data.svelte.ts', import.meta.url),
		'utf8'
	);
	// The cap remembers the track only while analysis was still pending.
	assert.match(
		data,
		/if \(!asset\.pending \|\| tries > PENDING_TRIES\) \{\s*_tries\.delete\(stable_id\);\s*if \(asset\.pending\) _capped\.add\(stable_id\);\s*return;/
	);
	// A revisit keeps the held strip on screen and fetches once more, without dropping to loading.
	const request = data.slice(
		data.indexOf('export function requestPreviewAssets'),
		data.indexOf('function _fetchInto')
	);
	assert.match(
		request,
		/_assets\.set\(stable_id, held\);\s*(?:\/\/[^\n]*\n\s*)*if \(_capped\.delete\(stable_id\)\) _fetchInto\(stable_id\);\s*return;/
	);
	// An evicted track forgets its cap with its entry.
	assert.match(request, /_assets\.delete\(oldest\);\s*_capped\.delete\(oldest\);/);
});

test('a pending strip polls only while the bar rests on it; a row left behind asks again on its next dwell (Sol r4278366209)', () => {
	const data = readFileSync(
		new URL('../../src/lib/rb/cmdk-preview-data.svelte.ts', import.meta.url),
		'utf8'
	);
	const retry = data.slice(
		data.indexOf('setTimeout(() => {'),
		data.indexOf('}, PENDING_RETRY_MS);')
	);
	// Only the watched track is asked for again; any other one stops and is capped for its next visit.
	assert.match(
		retry,
		/if \(_watched === stable_id\) _fetchInto\(stable_id\);\s*else \{\s*_tries\.delete\(stable_id\);\s*_capped\.add\(stable_id\);\s*\}/
	);
	assert.match(
		data,
		/export function watchPreview\(stable_id: string \| null\): void \{\s*_watched = stable_id;\s*\}/
	);
	// The bar names the row only once its dwell fires, and clears it as the highlight moves or the bar closes.
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	const dwell = bar.slice(
		bar.indexOf('// Ask for the overview only after a rest on the row'),
		bar.indexOf('function onPreviewPainted')
	);
	assert.match(
		dwell,
		/const id = previewId;\s*dwelled = null;\s*watchPreview\(null\);\s*(\/\/[^\n]*\s*)?dwellAt = 0;\s*dwellId = null;\s*firstPaintMs = null;\s*if \(id === null\) return;/
	);
	assert.match(dwell, /setTimeout\(\(\) => \{\s*dwelled = id;\s*watchPreview\(id\);/);
	assert.match(dwell, /return \(\) => \{\s*clearTimeout\(timer\);\s*watchPreview\(null\);\s*\};/);
});

test("the /anlz read waits for the row's own dwell, even when its strip is cached (Sol r4278366206)", () => {
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	// Untracked too: a tracked call rereads the cache entry it then rewrites and loops the effect
	// until Svelte aborts (effect_update_depth_exceeded), which tests/e2e/cmdk-preview-cue.spec.ts caught.
	assert.match(
		bar,
		/const id = previewId;\s*if \(id !== null && dwelled === id && previewData !== undefined && previewData\.status !== 'loading'\)\s*untrack\(\(\) => ensureAnlzPrefetch\(id\)\);/
	);
	assert.equal(
		(bar.match(/ensureAnlzPrefetch\(/g) ?? []).length,
		1,
		'no other path prefetches /anlz'
	);
});

test("the /beatgrid-fallback fetch waits for the row's own dwell, like /anlz (CMDK-07)", () => {
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		bar,
		/const id = previewId;\s*if \(id !== null && dwelled === id && shouldUseBeatgridFallback\(previewFallbackGate\)\) ensureBeatgridFallback\(id\);/
	);
	assert.equal(
		(bar.match(/ensureBeatgridFallback\(/g) ?? []).length,
		1,
		'no other path prefetches /beatgrid-fallback'
	);
});

test('typing while focus is off the input clears the highlight and the picker; picker keys win over typing (Sol r4278366215, r4278366223)', () => {
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	const win = bar.slice(
		bar.indexOf('function onWindowKeydown'),
		bar.indexOf('const pickLoad = createPickLoader')
	);
	const pick = win.indexOf(
		"previewMode !== 'off' && picker !== null && wrap?.contains(event.target as Node) && pickerKey(event)"
	);
	const typing = win.indexOf('query += event.key;');
	assert.ok(pick > 0 && pick < typing, 'the picker gets the key before it becomes search text');
	assert.match(
		win.slice(typing),
		/^query \+= event\.key;\s*(?:\/\/[^\n]*\s*)*selected = 0;\s*touched = false;\s*picker = null;\s*input\?\.focus\(\);/
	);
});

test('a replace guard answers only the load it asked about (Codex r4275059846)', () => {
	const asked = picker.replaceGuard(2, { stable_id: 'a', load_generation: 5 }, 9, 4);
	assert.deepEqual(asked, { id: 2, stable_id: 'a', load_generation: 5, loads: 9, commands: 4 });
	assert.equal(
		picker.guardHolds(asked, { stable_id: 'a', load_generation: 5 }, 9, 4),
		true,
		'control: nothing moved, the confirm stands'
	);
	assert.equal(
		picker.guardHolds(asked, { stable_id: 'b', load_generation: 6 }, 10, 4),
		false,
		'another track was loaded'
	);
	assert.equal(
		picker.guardHolds(asked, { stable_id: 'a', load_generation: 6 }, 10, 4),
		false,
		'the same track was loaded again'
	);
	assert.equal(
		picker.guardHolds(asked, { stable_id: null, load_generation: 5 }, 9, 4),
		false,
		'the deck was ejected'
	);
	// A load dispatched but still queued or decoding has not moved the deck yet (Codex r4277027005).
	assert.equal(
		picker.guardHolds(asked, { stable_id: 'a', load_generation: 5 }, 10, 4),
		false,
		'a newer load is on its way'
	);
	// A play that lands while the question is open starts the track, so the confirm asks again (Codex P1, #6689).
	assert.equal(
		picker.guardHolds(asked, { stable_id: 'a', load_generation: 5 }, 9, 5),
		false,
		'a play started the deck while the guard was open'
	);
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	// The guard is taken from the engine's deck state when it asks...
	assert.match(
		bar,
		/p\.guard = replaceGuard\(deck\.id, decks\[deck\.id\], deckLoadDispatches\(deck\.id\), deckCommands\(deck\.id\)\);/
	);
	// ...and the confirm is only a confirm while that load is still there; otherwise it asks again.
	const confirm = bar.slice(
		bar.indexOf('function confirmGuard'),
		bar.indexOf('function pickerKey')
	);
	// A moved deck is asked about again whether or not it plays now (Codex r4275496748): a paused
	// track loaded during the guard is never replaced on the old confirm.
	assert.match(
		confirm,
		/if \(!guardHolds\(guard, decks\[guard\.id\], deckLoadDispatches\(guard\.id\), deckCommands\(guard\.id\)\)\) \{\s*const session = barSession;\s*const again = await settledGuard\(guard\.id, session\);\s*(?:\/\/[^\n]*\s*)*if \(again !== null && picker === p && barSession === session && p\.guard === null\) p\.guard = again;\s*return;\s*\}\s*pickerLoad\(index, true\);/
	);
	// Asked again only once the loads still queued have landed, so it names what is there.
	const settled = bar.slice(
		bar.indexOf('async function settledGuard'),
		bar.indexOf('async function confirmGuard')
	);
	assert.match(
		settled,
		/const drained = await waitDeckIdle\(\s*\(\) => deckPending\(id\),[\s\S]*?\);[\s\S]*?if \(!drained\) \{\s*if \(barSession === session\) actionError = `Deck \$\{id\} is still busy; pick again once it settles`;\s*return null;\s*\}\s*return replaceGuard\(id, decks\[id\], deckLoadDispatches\(id\), deckCommands\(id\)\);/
	);
	// A queue that never drains asks nothing: no guard naming the old track (Codex r4277285054).
	assert.doesNotMatch(settled, /\n\t\tawait waitDeckIdle/);
	assert.doesNotMatch(confirm, /pickerLoad\(index, guardHolds/);
});

test('the preview strip repaints when the theme, wave palette or skin changes (Codex r4275811416)', () => {
	const strip = readFileSync(
		new URL('../../src/lib/components/rb/cmdk/CmdkPreviewStrip.svelte', import.meta.url),
		'utf8'
	);
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	// The bar keys all three prefs that swap the --rb-wave-* vars and hands the key to the strip.
	assert.match(
		bar,
		/const wavePaletteKey = \$derived\(`\$\{uiPrefs\.theme\}\|\$\{uiPrefs\.wave_palette\}\|\$\{uiPrefs\.ui_skin\}`\);/
	);
	assert.match(bar, /palette=\{wavePaletteKey\}/);
	// The paint effect reads the key before painting, so a change to it repaints.
	const effect = strip.slice(
		strip.indexOf('$effect(() => {\n\t\tif (canvas === null'),
		strip.indexOf('paint(canvas, width);')
	);
	assert.match(effect, /void palette;/);
	// Not an import: prefs.svelte's fan-in is a ratcheted quality metric.
	assert.doesNotMatch(strip, /from '\$lib\/rb\/prefs\.svelte'/);
});

test('a guard asked again describes the deck as it is now, not as playing (Codex r4276170535)', () => {
	const picker = readFileSync(
		new URL('../../src/lib/components/rb/cmdk/CmdkDeckPicker.svelte', import.meta.url),
		'utf8'
	);
	const box = picker.slice(
		picker.indexOf('class="guard-box"'),
		picker.indexOf('</div>\n\t</div>\n{/if}')
	);
	// Only a playing deck is called live and mid-mix; a paused or empty one says what it holds now.
	assert.match(
		box,
		/\{#if guarded\.playing\}\s*<h4 id="cmdk2-guard-title">Deck \{guarded\.id\} is playing live<\/h4>\s*<p>Loading here stops/
	);
	assert.match(
		box,
		/\{:else if guarded\.loaded\}\s*<h4 id="cmdk2-guard-title">Deck \{guarded\.id\} changed<\/h4>\s*<p>It now holds "\{guarded\.title \?\? 'another track'\}", paused\./
	);
	assert.match(
		box,
		/\{:else\}\s*<h4 id="cmdk2-guard-title">Deck \{guarded\.id\} changed<\/h4>\s*<p>It is empty now\.<\/p>/
	);
	assert.match(box, /\{guarded\.loaded \? 'Replace it' : 'Load it'\} · \{modKey\}Enter/);
	// The platform's own modifier, as the eject guard shows it (Codex r4277285059).
	assert.doesNotMatch(box, /⌘Enter/);
	assert.match(
		readFileSync(
			new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
			'utf8'
		),
		/modKey=\{quickLabel\.trim\(\)\}/
	);
	assert.match(box, /\{guarded\.playing \? 'Keep playing' : 'Cancel'\} · Esc/);
});

test('CMDK-04/09: the deck picker lets Tab cycle focus and Backspace edit the search from a picker row (Sol review of 8e53c84f1)', () => {
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	const body = bar.slice(bar.indexOf('function pickerKey('), bar.indexOf('function deckList('));
	// Tab first, before the guard and the catch-all that swallows every other key.
	assert.match(
		body,
		/if \(event\.key === 'Tab'\) \{\s*cycleFocus\(event\.shiftKey\);\s*return true;\s*\}/
	);
	assert.ok(body.indexOf("event.key === 'Tab'") < body.indexOf('if (p.guard !== null)'));
	// From a picker row the input never sees Backspace, so the picker deletes from the query itself.
	assert.match(
		body,
		/if \(event\.key === 'Backspace' && event\.target !== input\) \{[^}]*picker = null;\s*query = \[\.\.\.query\]\.slice\(0, -1\)\.join\(''\);[^}]*input\?\.focus\(\);\s*return true;\s*\}/
	);
	// Control: with focus in the input, Backspace still falls through to the input's own edit.
	assert.match(
		body,
		/if \(event\.key === 'Backspace' \|\| \(event\.key\.length === 1[^\n]*\) \{\s*picker = null;\s*return false;\s*\}/
	);
	assert.ok(
		body.indexOf('event.target !== input') < body.indexOf("if (event.key === 'Backspace' ||")
	);
});

test('CMDK-07: the first-paint time counts only a paint of the row its own dwell asked for (Sol review of 8e53c84f1)', () => {
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		bar,
		/if \(firstPaintMs === null && dwellAt > 0 && dwellId !== null && dwellId === previewId\) firstPaintMs = performance\.now\(\) - dwellAt;/
	);
	const effect = bar.slice(
		bar.indexOf('const id = previewId;\n\t\tdwelled = null;'),
		bar.indexOf('}, PREVIEW_DWELL_MS);')
	);
	// The highlight moving clears the stamp at once, and the dwell sets the stamp and its row together.
	assert.ok(effect.indexOf('dwellAt = 0;') < effect.indexOf('if (id === null) return;'));
	assert.match(effect, /dwellAt = performance\.now\(\);\s*dwellId = id;/);
});

test('CMDK-07: resting the full dwell asks strip assets once; nothing fires inside the interval', () => {
	const bar = readCommandBarSource();
	assert.equal(parsePreviewDwellMs(bar), 90);
	const h = createPreviewDwellHarness(bar);
	const id = 'a'.repeat(40);
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		h.setPreviewId(id);
		const cleanup = h.run();
		assert.equal(typeof cleanup, 'function');
		mock.timers.tick(h.PREVIEW_DWELL_MS - 1);
		assert.equal(h.calls.requestPreviewAssets.length, 0);
		assert.equal(h.getState().dwelled, null);
		mock.timers.tick(1);
		assert.equal(h.calls.requestPreviewAssets.length, 1);
		assert.equal(h.calls.requestPreviewAssets[0], id);
		assert.equal(h.calls.watchPreview.at(-1), id);
		assert.equal(h.getState().dwelled, id);
		cleanup();
	} finally {
		mock.timers.reset();
	}
});

test('CMDK-07: arrowing to another row before dwell fetches only the row that rested', () => {
	const bar = readCommandBarSource();
	const h = createPreviewDwellHarness(bar);
	const idA = 'a'.repeat(40);
	const idB = 'b'.repeat(40);
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		h.setPreviewId(idA);
		let cleanup = h.run();
		mock.timers.tick(50);
		cleanup();
		h.setPreviewId(idB);
		cleanup = h.run();
		mock.timers.tick(h.PREVIEW_DWELL_MS);
		assert.ok(!h.calls.requestPreviewAssets.includes(idA));
		assert.equal(h.calls.requestPreviewAssets.length, 1);
		assert.equal(h.calls.requestPreviewAssets[0], idB);
		cleanup();
	} finally {
		mock.timers.reset();
	}
});

test('CMDK-07: closing the bar before dwell fetches nothing', () => {
	const bar = readCommandBarSource();
	const h = createPreviewDwellHarness(bar);
	const id = 'a'.repeat(40);
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		h.setPreviewId(id);
		const cleanup = h.run();
		mock.timers.tick(50);
		cleanup();
		h.setPreviewId(null);
		h.run();
		mock.timers.tick(h.PREVIEW_DWELL_MS * 2);
		assert.equal(h.calls.requestPreviewAssets.length, 0);
		assert.equal(h.calls.watchPreview.at(-1), null);
		assert.equal(h.getState().dwelled, null);
	} finally {
		mock.timers.reset();
	}
});

test('CMDK-10: the inline overview draws every layer and the cue status, scaled to the row (Sol review of 8e53c84f1)', () => {
	const strip = readFileSync(
		new URL('../../src/lib/components/rb/cmdk/CmdkPreviewStrip.svelte', import.meta.url),
		'utf8'
	);
	const inline =
		/: \{ H: (\d+), ribbonH: (\d+), waveTop: (\d+), waveH: (\d+), vocalTop: (\d+), vocalH: (\d+), flag: (\d+), font: (\d+), label: 0 \}/.exec(
			strip
		);
	assert.ok(inline, 'the inline geometry');
	const [H, ribbonH, waveTop, waveH, vocalTop, vocalH, flag] = inline.slice(1).map(Number);
	assert.ok(ribbonH > 0, 'the section ribbon is drawn inline');
	assert.ok(
		waveTop >= ribbonH && vocalTop >= waveTop + waveH && vocalTop + vocalH <= H,
		'the layers stack inside the strip'
	);
	assert.ok(flag > 0 && flag <= waveH);
	// Cue letters are drawn in both layouts, not only above the bar.
	assert.doesNotMatch(strip, /if \(variant === 'above'\) \{\s*ctx\.fillRect\(x - 0\.5, g\.waveTop/);
	assert.match(
		strip,
		/ctx\.fillText\(cue\.slot as string, x \+ g\.flag \/ 2 - 0\.5, g\.waveTop \+ g\.flag \/ 2 \+ 0\.5\);/
	);
	assert.match(
		strip,
		/\{#if variant === 'inline' && message !== ''\}\s*<!--[^>]*-->\s*<span class="pv-msg inline-msg" data-testid="command-bar-preview-inline-message">\{message\}<\/span>/
	);
	// Control: the strip above the bar keeps its labeled ribbon and header message.
	assert.match(
		strip,
		/\? \{ H: 64, ribbonH: 13, waveTop: 15, waveH: 41, vocalTop: 59, vocalH: 5, flag: 11, font: 9, label: 9 \}/
	);
	assert.match(strip, /data-testid="command-bar-preview-message">\{message\}/);
});

test('a start answered after a stop is stopped, even once a newer jump has started (Sol r4280385212)', async () => {
	const A = 'a'.repeat(40);
	const B = 'b'.repeat(40);
	const sent = [];
	let sounding = null;
	let releaseA;
	const gateA = new Promise((r) => (releaseA = r));
	const cue = cuePreview.createCuePreview({
		dispatch: async (command) => {
			sent.push(command.type === 'preview_stop' ? 'stop' : command.stable_id[0]);
			if (command.type === 'preview_stop') {
				sounding = null;
				return true;
			}
			if (command.stable_id === A) {
				await gateA;
				sounding = A;
				return true;
			}
			return false; // B is refused, leaving whatever was sounding in place
		},
		masterPhase: () => 0.25,
		sounding: (id) => sounding === id,
		now: () => 0
	});
	cue.setTrack(cueTrack(A));
	const first = cue.jump(1, false);
	cue.setTrack(cueTrack(B));
	const second = cue.jump(1, false);
	releaseA();
	await Promise.all([first, second]);
	assert.deepEqual(sent, ['a', 'stop', 'b'], "A's late start is stopped before B is sent");
	assert.equal(sounding, null, 'nothing is left sounding when B is refused');
	// Control: a same-track jump that supersedes a start without a stop sends no stop.
	const h = harness();
	h.cue.setTrack(cueTrack());
	const release = h.holdNext();
	const one = h.cue.jump(1, false);
	const two = h.cue.jump(1, false);
	release();
	await Promise.all([one, two]);
	assert.ok(!h.sent.some((c) => c.type === 'preview_stop'), 'no stop without a stop');
});

test('a playing master whose phase cannot be read refuses the preview with a named reason; no master starts on the downbeat (Sol r4280385227)', async () => {
	const h = harness({ phase: 'unknown' });
	h.cue.setTrack(cueTrack());
	await h.cue.jump(1, false);
	assert.deepEqual(h.sent, [], 'nothing is sent out of phase');
	assert.equal(h.cue.state.message, cuePreview.MASTER_PHASE_UNKNOWN);
	assert.equal(h.cue.state.started, false);
	// Control: with no master playing, it starts at bar 1's downbeat.
	const none = harness({ phase: null });
	const t = cueTrack();
	none.cue.setTrack(t);
	await none.cue.jump(1, false);
	assert.equal(none.sent.length, 1);
	assert.ok(
		Math.abs(none.sent[0].ratio - at(0, 0) / t.duration_s) < 1e-12,
		'bar 1, on the downbeat'
	);
	// The bar reports a playing master with no readable phase as unknown, never as no master.
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		bar,
		/if \(master === undefined\) return null;[\s\S]{0,400}return phase \?\? 'unknown';/
	);
});

test('the strip shows bank slots A to F by letter, hot loops included (Sol r4280385244)', () => {
	const cue = (slot, kind, extra = {}) => ({
		slot,
		kind,
		in_ms: 1000,
		out_ms: null,
		is_loop: false,
		active_loop: false,
		beat_loop_size: null,
		color_table_index: null,
		comment: null,
		...extra
	});
	const shown = grid.stripHotCues([
		cue('C', 'hot_cue'),
		cue('A', 'loop', { is_loop: true, out_ms: 3000 }),
		cue(null, 'memory'),
		cue(null, 'loop', { is_loop: true, out_ms: 3000 }),
		cue('G', 'hot_cue')
	]);
	assert.deepEqual(
		shown.map((c) => c.slot),
		['A', 'C'],
		'the hot loop in slot A is shown; memory cues, plain loops and G are not'
	);
	const strip = readFileSync(
		new URL('../../src/lib/components/rb/cmdk/CmdkPreviewStrip.svelte', import.meta.url),
		'utf8'
	);
	assert.match(strip, /stripHotCues\(anlz\.cues\)/);
	assert.match(strip, /ctx\.fillStyle = cue\.is_loop \? palette\.cueLoop : palette\.cueHotCue;/);
});

test('a deck load mark needs the deck play state: it is not optional (Sol r4280385219)', () => {
	const load = readFileSync(new URL('../../src/lib/rb/cmdk-load.ts', import.meta.url), 'utf8');
	const iface = load.slice(
		load.indexOf('export interface DeckLoadState'),
		load.indexOf('}', load.indexOf('export interface DeckLoadState'))
	);
	assert.match(iface, /\n\tplaying: boolean;/);
	assert.doesNotMatch(iface, /playing\?:/);
});

test('a press before the beat grid has loaded is held and starts the preview once the grid arrives (Codex r4280683906)', async () => {
	const h = harness();
	const t = cueTrack();
	h.cue.setTrack({ ...t, downbeats: [], stops: [], gridPending: true });
	await h.cue.jump(1, false);
	assert.deepEqual(h.sent, [], 'nothing to seek against yet');
	assert.equal(h.cue.state.message, cuePreview.GRID_LOADING);
	// The grid arrives for the same track: the held press starts it at bar 1, no second press needed.
	h.cue.setTrack(t);
	await new Promise((r) => setTimeout(r, 0));
	assert.equal(h.sent.length, 1);
	assert.equal(h.sent[0].type, 'preview_cue');
	assert.equal(h.cue.state.message, 'Bar 1');
	// A grid read that ends with no grid says so and plays nothing.
	const none = harness();
	none.cue.setTrack({ ...t, downbeats: [], stops: [], gridPending: true });
	await none.cue.jump(1, false);
	none.cue.setTrack({ ...t, downbeats: [], stops: [], gridPending: false });
	assert.deepEqual(none.sent, []);
	assert.match(none.cue.state.message, /No beat grid yet/);
	// Moving to another track, or Esc, drops the held press.
	const moved = harness();
	moved.cue.setTrack({ ...t, downbeats: [], stops: [], gridPending: true });
	await moved.cue.jump(1, false);
	moved.cue.setTrack(cueTrack('d'.repeat(40)));
	const esc = harness();
	esc.cue.setTrack({ ...t, downbeats: [], stops: [], gridPending: true });
	await esc.cue.jump(1, false);
	esc.cue.stop();
	esc.cue.setTrack(t);
	await new Promise((r) => setTimeout(r, 0));
	assert.deepEqual(moved.sent, [], 'a held press never plays another track');
	assert.deepEqual(esc.sent, [], 'Esc cancels a held press');
	// The bar holds the press while /anlz or its gated fallback is still loading,
	// but stops waiting once either lane has a terminal answer.
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		bar,
		/if \(previewAnlzEntry === undefined \|\| previewAnlzEntry\.status === 'loading'\) return true;/
	);
	assert.match(bar, /if \(!shouldUseBeatgridFallback\(previewFallbackGate\)\) return false;/);
	assert.match(bar, /return fallbackEntry === undefined \|\| fallbackEntry\.status === 'loading';/);
	assert.match(bar, /gridPending: previewGridPending/);
	for (const productionSymbol of [
		'shouldUseBeatgridFallback',
		'ensureBeatgridFallback',
		'getBeatgridFallbackEntry',
		'readyBeatgridFallback',
		'paintAnlzForRow'
	]) {
		assert.ok(bar.includes(productionSymbol), `CommandBarV2 must use ${productionSymbol}`);
	}
	assert.doesNotMatch(bar, /PreviewFallbackEntry|cmdk-preview-beatgrid/);
});

test('a refused corrective reseek stops the first source instead of leaving it at the stale phase', async () => {
	const h = harness();
	h.cue.setTrack(cueTrack());
	const release = h.holdNext();
	const pending = h.cue.jump(1, false);
	h.tick(cuePreview.COLD_START_RESEEK_MS + 200);
	h.refuseSend(1);
	release();
	await pending;
	assert.deepEqual(
		h.sent.map((c) => c.type),
		['preview_cue', 'preview_cue', 'preview_stop']
	);
	assert.equal(
		h.cue.state.started,
		false,
		'the controller gives up ownership of the stopped source'
	);
});

test('Esc during a seek that is then refused still stops the sounding preview', async () => {
	const h = harness();
	h.cue.setTrack(cueTrack());
	await h.cue.jump(1, false);
	const release = h.holdNext();
	const seek = h.cue.jump(1, false);
	assert.equal(h.cue.stop(), true);
	h.refuseSend(1);
	release();
	await seek;
	assert.equal(
		h.sent.at(-1).type,
		'preview_stop',
		'the deferred stop is sent whatever the seek answered'
	);
});

test('← after a Shift jump returns to the bar the jump came from, whatever the grid size', async () => {
	const h = harness();
	h.cue.setTrack(cueTrack());
	await h.cue.jump(1, false);
	const before = h.cue.state.anchor;
	await h.cue.jump(1, true);
	assert.notEqual(h.cue.state.anchor, before, 'the Shift jump moved');
	// A plain ← from the Shift target walks the 4-bar grid to another bar, not back to the origin.
	await h.cue.jump(-1, false);
	assert.equal(h.cue.state.anchor, before, 'the reverse press retraces the jump');
});

test('Esc on a press held behind the beat grid is consumed: it cancels that press and is not passed on (Sol P2, #6689)', async () => {
	const h = harness();
	const t = cueTrack();
	h.cue.setTrack({ ...t, downbeats: [], stops: [], gridPending: true });
	await h.cue.jump(1, false);
	assert.equal(h.cue.stop(), true, 'the held press is preview work, so Esc is consumed');
	assert.equal(h.cue.stop(), false, 'with nothing held or sounding, Esc is not consumed');
	assert.deepEqual(h.sent, [], 'and the cancelled press sends nothing');
});

test('a queued jump re-reads the live grid before it starts, so a refreshed grid cannot be read through the old track (Sol P2, #6689)', () => {
	const src = readFileSync(
		new URL('../../src/lib/rb/cmdk-cue-preview.svelte.ts', import.meta.url),
		'utf8'
	);
	const send = src.slice(
		src.indexOf('const send = async'),
		src.indexOf('/** After a start returns')
	);
	// Control: the live track is read inside send, which runs after the queue waits, and the start is computed from it, not from `current`.
	assert.ok(send.indexOf('const live = track;') > 0);
	assert.match(send, /job\.bar >= live\.downbeats\.length/);
	assert.match(send, /inPhaseStartSec\(live\.downbeats, job\.bar, phase \?\? 0\)/);
	assert.doesNotMatch(send, /inPhaseStartSec\(current\.downbeats/);
});

test('X and Escape both cancel a replacement guard, so the guarded load never starts (Sol P1, #6687)', () => {
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	const guard = bar.slice(
		bar.indexOf('if (p.guard !== null) {'),
		bar.indexOf('const arrow = pickerArrow(event);')
	);
	// Control: the guard branch is the one that consumes the key, so its cancel is what the test reads.
	assert.match(
		guard,
		/if \(event\.key === 'Enter' && quickModifierHeld\(event, quickMod\)\) void confirmGuard\(\);/
	);
	assert.match(guard, /event\.key === 'Escape'/);
	assert.match(guard, /event\.key === 'x' \|\| event\.key === 'X'\) p\.guard = null;/);
});

test('a picker pick is refused while its deck still has a command queued, so no replace is approved over a play on its way (Codex P1 BLOCKING, #6687)', () => {
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	const load = bar.slice(
		bar.indexOf('function pickerLoad('),
		bar.indexOf('function confirmGuard(')
	);
	// Control: the refusal comes before the action is read, so a pick on a busy deck never reaches the replace guard.
	assert.match(
		load,
		/if \(deckPending\(deck\.id\)\) \{\s*pickerNote = `Deck \$\{deck\.id\} is still busy; pick again once it settles`;\s*return;\s*\}/
	);
	assert.ok(load.indexOf('deckPending(deck.id)') < load.indexOf('pickerAction(deck, confirmed)'));
	// The bar takes the count as a prop, as it takes the dispatcher, and the page passes it.
	assert.match(bar, /deckPending: \(deck: DeckId\) => number;/);
	assert.match(readFileSync(new URL('../../src/lib/components/rb/CommandPaletteHost.svelte', import.meta.url), 'utf8'), /deckPending=\{\(deck\) => performanceCommandStatus\.deck_pending\[deck\]\}/);
});

test('CMDK-19: a pick refuses when a command is queued on its deck during the rb-meta settle (Codex P1, #6687)', () => {
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	assert.match(bar, /pending: \(deck\) => deckPending\(deck\),/);
	const loader = readFileSync(new URL('../../src/lib/rb/cmdk-load.ts', import.meta.url), 'utf8');
	assert.match(loader, /if \(!newer && queue\.pending\(deck\) > 0\) \{/);
	assert.ok(
		loader.indexOf('settleForLoad(row, fetchMeta)') <
			loader.indexOf('if (!newer && queue.pending(deck) > 0)'),
		'the queued-command check runs after the settle'
	);
});

test('an untrusted beat grid is refused by name, never derived into bars (Sol P1, #6687)', () => {
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	const cue = readFileSync(
		new URL('../../src/lib/rb/cmdk-cue-preview.svelte.ts', import.meta.url),
		'utf8'
	);
	assert.match(
		bar,
		/hit = anlz\.beatgrid\.bar_phase_untrusted === true \? \[\] : downbeatTimes\(beats\);/
	);
	assert.match(
		cue,
		/state\.message = current\.untrusted === true \? BAR_PHASE_UNTRUSTED : 'No beat grid yet, so it cannot jump by bars';/
	);
	assert.match(
		cue,
		/export const BAR_PHASE_UNTRUSTED = "The beat grid's bar numbers are untrusted/
	);
});

test('a refused jump puts back the bar that still sounds (Sol P2, #6687)', () => {
	const cue = readFileSync(
		new URL('../../src/lib/rb/cmdk-cue-preview.svelte.ts', import.meta.url),
		'utf8'
	);
	assert.match(
		cue,
		/const before = \{ anchor: state\.anchor, history: \[\.\.\.history\], message: state\.message \};/
	);
	assert.match(
		cue,
		/const restore: JumpRestore = fresh \? null : \{ \.\.\.before, landed: state\.message \};/
	);
	assert.match(
		cue,
		/else if \(job\.restore !== null\) \{\s*state\.anchor = job\.restore\.anchor;\s*history = job\.restore\.history;/
	);
});

test('a stop is sent only for a source this controller owns (Sol P2, #6687)', () => {
	const cue = readFileSync(
		new URL('../../src/lib/rb/cmdk-cue-preview.svelte.ts', import.meta.url),
		'utf8'
	);
	assert.match(
		cue,
		/if \(stoppedAt > job\.ask && \(started \|\| job\.owned\)\) await ports\.dispatch\(\{ type: 'preview_stop' \}\);/
	);
});

test('the cue status shows only while its preview sounds (Sol P2, #6687)', () => {
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		bar,
		/const cueSounding = \$derived\(cue\.state\.started && previewCue\.playing && previewCue\.stable_id === previewId\);/
	);
	assert.match(bar, /if \(!cueSounding \|\| cueTrack === null/);
});

test('an eject that waits for its deck to drain is superseded by a newer eject, so it never overwrites that guard (Codex P2, #6689)', () => {
	const bar = readFileSync(
		new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url),
		'utf8'
	);
	const eject = bar.slice(
		bar.indexOf('const op = ++ejectOp;'),
		bar.indexOf('ejectGuard = replaceGuard(command.deck')
	);
	// Control: the token is taken before the drain, and checked after it, before the guard or the unload.
	assert.match(eject, /if \(op !== ejectOp\) return `/);
	assert.ok(eject.indexOf('const op = ++ejectOp;') < eject.indexOf('waitDeckIdle('));
});
