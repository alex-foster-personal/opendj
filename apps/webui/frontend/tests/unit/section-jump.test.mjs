import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Section jump (issue #10263): next and previous section start, on a downbeat.
 *
 * Regression lines:
 * - if "previous" inside the 1 s grace re-lands on the section just reached then broken
 * - if "next" lands off the grid when a downbeat is near then broken
 * - if a deck with no phrases plans a seek instead of NO_SECTIONS then broken
 * - if a section that snaps behind the playhead is picked as "next" then broken
 */

let sj;

before(async () => {
	sj = await loadTypeScriptModule('src/lib/rb/section-jump.ts');
});

/** 120 BPM 4/4 from 0 s: beats every 0.5 s, downbeats every 2 s, `bars` bars. */
function grid(bars, first = 0) {
	return Array.from({ length: bars * 4 }, (_, i) => ({
		t: Math.round((first + i * 0.5) * 1000) / 1000,
		n: (i % 4) + 1,
		bpm: 120
	}));
}
const BEATS = grid(48);
const DOWNBEATS = BEATS.filter((b) => b.n === 1).map((b) => b.t);
const SECTIONS = [0, 30, 60];
const target = (positionSec, direction, sections = SECTIONS) =>
	sj.sectionJumpTarget(positionSec, direction, sections, DOWNBEATS);

test('downbeats are every 2 s on the 120 BPM grid', () => {
	assert.deepEqual(sj.downbeatTimesSec(BEATS), DOWNBEATS);
	assert.equal(DOWNBEATS.length, 48);
	assert.deepEqual(DOWNBEATS.slice(0, 3), [0, 2, 4]);
});

test('previous inside the 1 s grace goes to the section just started; past it, one further back', () => {
	assert.equal(target(31.2, 'previous'), 30);
	assert.equal(target(30.4, 'previous'), 0);
});

test('next from 31.2 s lands on 60 s, snapped to the nearest downbeat', () => {
	assert.equal(target(31.2, 'next'), 60);
	// A start off the grid snaps to its nearest downbeat (61.3 s -> 62 s).
	assert.equal(target(31.2, 'next', [0, 30, 61.3]), 62);
});

test('a start that snaps behind the playhead is never a forward target', () => {
	// 30.6 s snaps to 30 s, which is behind 31.2 s, so next skips to 60 s.
	assert.equal(target(31.2, 'next', [0, 30.6, 60]), 60);
});

test('nothing ahead or behind answers null; with no grid the raw starts are used', () => {
	assert.equal(target(61, 'next'), null);
	assert.equal(target(0, 'previous'), null);
	assert.equal(target(0.5, 'previous'), 0, 'inside the first section: back to its start');
	assert.equal(sj.sectionJumpTarget(10, 'next', [0, 12.7, 40], []), 12.7);
});

test('bad input is refused loudly', () => {
	assert.throws(() => target(Number.NaN, 'next'), RangeError);
	assert.throws(() => target(10, 'sideways'), RangeError);
});

const phrase = (start_s, end_s) => ({ start_s, end_s, kind: 0, mood: 0 });

test('planSectionJump: no phrases is refused with NO_SECTIONS and no seek', () => {
	const plan = sj.planSectionJump({
		positionSec: 31.2,
		direction: 'next',
		phrases: [],
		beats: BEATS
	});
	assert.deepEqual(plan, { kind: 'refused', reason: 'NO_SECTIONS' });
});

test('planSectionJump: a seek plan carries the downbeat target in ms', () => {
	const phrases = [phrase(0, 30), phrase(30, 60), phrase(60, 96)];
	assert.deepEqual(
		sj.planSectionJump({ positionSec: 31.2, direction: 'next', phrases, beats: BEATS }),
		{ kind: 'seek', targetMs: 60000 }
	);
	assert.deepEqual(
		sj.planSectionJump({ positionSec: 30.4, direction: 'previous', phrases, beats: BEATS }),
		{ kind: 'seek', targetMs: 0 }
	);
	assert.deepEqual(
		sj.planSectionJump({ positionSec: 70, direction: 'next', phrases, beats: BEATS }),
		{ kind: 'refused', reason: 'NO_SECTION_AHEAD' }
	);
});
