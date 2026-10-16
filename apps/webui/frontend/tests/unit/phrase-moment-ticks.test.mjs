/**
 * STRUCT-07: drop and break moments as deck-strip ticks (phrase-moment-ticks.ts).
 *
 * Regression lines:
 * - if a numbered drop does not get a "D<n>" tick at its time then broken
 * - if a fake drop or a build gets a tick then broken: the strip would show a
 *   drop that is not one, or clutter the top edge
 * - if a tick has no hover title naming time and bar then broken
 * - if a track with no moments, or no duration, draws ticks then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { phraseMomentTicks } from '../../src/lib/rb/phrase-moment-ticks.ts';

const MOMENTS = [
	{ kind: 'build', time_s: 30, bar: 16 },
	{ kind: 'drop', time_s: 40, bar: 20, number: null, fake: true },
	{ kind: 'drop', time_s: 60, bar: 32, number: 1, fake: false },
	{ kind: 'break', time_s: 120, bar: 64 },
	{ kind: 'drop', time_s: 180, bar: 96, number: 2, fake: false }
];

test('STRUCT-07: numbered drops and breaks get ticks; fake drops and builds do not', () => {
	const ticks = phraseMomentTicks(MOMENTS, 240_000);
	assert.deepEqual(
		ticks.map((t) => [t.kind, t.text, t.leftPct]),
		[
			['drop', 'D1', 25],
			['break', 'B', 50],
			['drop', 'D2', 75]
		]
	);
	assert.equal(
		ticks[0].title,
		'Drop 1 at 01:00 (bar 33): the first downbeat where drums and bass come back after a break or build'
	);
	assert.match(ticks[1].title, /^Break at 02:00 \(bar 65\)/);
});

test('STRUCT-07: no moments or no duration draws nothing', () => {
	assert.deepEqual(phraseMomentTicks(undefined, 240_000), []);
	assert.deepEqual(phraseMomentTicks([], 240_000), []);
	assert.deepEqual(phraseMomentTicks(MOMENTS, null), []);
	assert.deepEqual(phraseMomentTicks(MOMENTS, 0), []);
});
