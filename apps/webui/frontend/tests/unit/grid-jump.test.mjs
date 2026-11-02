import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * DECKUX-6794: snapped deck jumps land on a fixed bar grid, so Right then Left
 * replays the same passage ((private path) AC-1..AC-4, AC-13).
 *
 * Regression lines:
 * - if a jump counts from the playhead instead of the grid then broken
 * - if Right then Left does not return to the block it left then broken
 * - if a step jumps over a section start then broken
 * - if a playing deck loses its beat-in-bar then broken (a synced deck would
 *   leave the master's downbeats)
 * - if a grid with no downbeats jumps anyway then broken
 */

let gj;
let math;

before(async () => {
	gj = await loadTypeScriptModule('src/lib/player/transport/grid-jump.ts');
	math = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
});

/** 125 BPM (0.48 s beats) from 0.5 s, 4/4, `bars` bars. */
function grid(bars, first = 0.5) {
	return Array.from({ length: bars * 4 }, (_, i) => ({
		t: Math.round((first + i * 0.48) * 1000) / 1000,
		n: (i % 4) + 1,
		bpm: 125
	}));
}
const BEATS = grid(64);
/** Track ms of 1-based bar `bar` plus `beat` beats (fractional ok). */
const at = (bar, beat = 0) => (0.5 + ((bar - 1) * 4 + beat) * 0.48) * 1000;

function plan(anchorMs, direction, { playing = false, stepBars = 4, sections = [] } = {}) {
	return gj.planGridJump({ beats: BEATS, sectionStartsSec: sections, anchorMs, direction, stepBars, playing });
}

/** Where the engine's beat jump lands for a plan (the same math it runs). */
function land(anchorMs, p, playing) {
	assert.equal(p.kind, 'jump', JSON.stringify(p));
	return math.beatJumpTargetMs(BEATS, anchorMs, p.deltaBeats, playing);
}

test('AC-1: paused at bar 6.3, Right lands on bar 9, then Left on bar 5', () => {
	const start = at(6, 1.2);
	const right = plan(start, 1);
	assert.equal(right.targetBar, 9);
	const afterRight = land(start, right, false);
	assert.equal(afterRight, at(9));
	const left = plan(afterRight, -1);
	assert.equal(left.targetBar, 5);
	assert.equal(land(afterRight, left, false), at(5));
});

test('Left inside a block replays that block from its start', () => {
	const p = plan(at(7, 2), -1);
	assert.equal(p.targetBar, 5);
});

test('control: today\'s beat jump from the same spot does NOT return (it drifts with the playhead)', () => {
	// +16 then -16 beats after 3 beats of play lands 3 beats later than it started.
	const start = at(6, 1);
	const out = math.beatJumpTargetMs(BEATS, start, 16, true);
	const back = math.beatJumpTargetMs(BEATS, out + 3 * 480, -16, true);
	assert.notEqual(back, start);
	// The snapped jump from the same drifted spot lands on the block start.
	const snapped = plan(out + 3 * 480, -1, { playing: true });
	assert.equal(snapped.targetBar, 9);
});

test('AC-2: a playing deck keeps its beat-in-bar and phase across the jump', () => {
	const start = at(6, 2.25); // bar 6, beat 3, a quarter through it
	const p = plan(start, 1, { playing: true });
	assert.equal(p.targetBar, 9);
	assert.equal(p.deltaBeats, 12);
	const landed = land(start, p, true);
	assert.ok(Math.abs(landed - at(9, 2.25)) < 1e-6, `${landed}`);
});

test('AC-3: an 8-bar step', () => {
	assert.equal(plan(at(2), 1, { stepBars: 8 }).targetBar, 9);
	assert.equal(plan(at(9), 1, { stepBars: 8 }).targetBar, 17);
});

test('AC-4: a step stops at a section start it would pass, then counts on from it', () => {
	const sections = [at(1) / 1000, at(31) / 1000];
	assert.equal(plan(at(29), 1, { sections }).targetBar, 31);
	const p = plan(at(29), 1, { sections });
	assert.equal(p.sectionStart, true);
	assert.equal(plan(at(31, 1), 1, { sections }).targetBar, 35);
	assert.equal(plan(at(25), 1, { sections }).sectionStart, false);
});

test('section starts snap to their nearest downbeat', () => {
	const sections = [at(31, 0.4) / 1000];
	assert.equal(plan(at(29), 1, { sections }).targetBar, 31);
});

test('before bar 1, both directions go to bar 1', () => {
	const beats = grid(16, 2.0);
	const right = gj.planGridJump({ beats, sectionStartsSec: [], anchorMs: 2000 - 0, direction: 1, stepBars: 4, playing: false });
	// At the first downbeat already: Right goes on to bar 5.
	assert.equal(right.targetBar, 5);
	const shifted = beats.map((b, i) => ({ ...b, n: ((i + 2) % 4) + 1 })); // two pickup beats
	const left = gj.planGridJump({ beats: shifted, sectionStartsSec: [], anchorMs: 2100, direction: -1, stepBars: 4, playing: false });
	assert.equal(left.kind, 'jump');
	assert.equal(left.targetBar, 1);
});

test('edges refuse with a reason instead of moving', () => {
	assert.equal(plan(at(62), 1).targetBar, 64);
	const last = plan(at(64, 1), 1);
	assert.equal(last.kind, 'refused');
	assert.match(last.reason, /last block/);
	const first = plan(at(1), -1);
	assert.equal(first.kind, 'refused');
	assert.match(first.reason, /already at bar 1/);
});

test('AC-13: a grid with fewer than two bars refuses, distinct from an empty deck', () => {
	const p = gj.planGridJump({ beats: grid(1), sectionStartsSec: [], anchorMs: at(1, 1), direction: 1, stepBars: 4, playing: false });
	assert.equal(p.kind, 'refused');
	assert.match(p.reason, /no bar grid/);
});

test('a playhead outside the grid refuses', () => {
	const p = plan(at(70), 1);
	assert.equal(p.kind, 'refused');
	assert.match(p.reason, /outside the beatgrid/);
});

test('gridJumpPoints matches the Cmd K grid() rule', () => {
	assert.deepEqual(gj.gridJumpPoints(20, [], 4), [0, 4, 8, 12, 16, 19]);
	assert.deepEqual(gj.gridJumpPoints(20, [6], 4), [0, 4, 6, 10, 14, 18, 19]);
	assert.throws(() => gj.gridJumpPoints(20, [], 0), RangeError);
});

test('a lead-in before the first beat: Right seeks to bar 1, Left refuses', () => {
	const pre = gj.planGridJump({ beats: BEATS, sectionStartsSec: [], anchorMs: 0, direction: 1, stepBars: 4, playing: false });
	assert.deepEqual(pre, { kind: 'seek', targetMs: 500, targetBar: 1 });
	const back = gj.planGridJump({ beats: BEATS, sectionStartsSec: [], anchorMs: 0, direction: -1, stepBars: 4, playing: false });
	assert.equal(back.kind, 'refused');
	assert.match(back.reason, /outside the beatgrid/);
});
