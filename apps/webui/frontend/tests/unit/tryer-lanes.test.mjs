// requirement: TRYER-16
// The try-er's two-deck lanes: curve values at span edges, curves cut to play spans, peak resampling.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let lanes;

before(async () => {
	lanes = await loadTypeScriptModule('src/lib/tryer/tryer-lanes.ts');
});

// An incoming fader as the route sends it: power-on 1.0, set to 0 at 9.375 s, ramped to 0.5 by 11.25 s.
const FADER = [
	[0, 1],
	[9.375, 1],
	[9.375, 0],
	[9.375, 0],
	[11.25, 0.5]
];

test('a span starting on a step takes the value after the step, so a silent deck never draws its power-on line', () => {
	assert.equal(
		lanes.valueAt(FADER, 9.375, 'after'),
		0,
		'if a span starts at 1.0 then the fader appears up before it opens'
	);
	assert.equal(
		lanes.valueAt(FADER, 9.375, 'before'),
		1,
		'if before-step reads 0 then a span ending on a step loses its level'
	);
	assert.equal(
		lanes.valueAt(FADER, 10.3125, 'after'),
		0.25,
		'if mid-ramp is not interpolated then lines step instead of fade'
	);
	assert.equal(
		lanes.valueAt(FADER, 99, 'after'),
		0.5,
		'if past the last breakpoint is not held then lines vanish'
	);
});

test('curves are cut to each play span with interpolated endpoints', () => {
	const [segment] = lanes.segmentsWithin(FADER, [[9.375, 20]]);
	assert.deepEqual(
		segment[0],
		[9.375, 0],
		'if the segment starts above 0 then the incoming deck looks open before it is'
	);
	assert.deepEqual(segment.at(-1), [20, 0.5]);
	assert.ok(
		segment.every(([t]) => t >= 9.375 && t <= 20),
		'if points outside the span survive then lines draw over silence'
	);
	assert.deepEqual(
		lanes.segmentsWithin(FADER, []),
		[],
		'if a deck that never plays gets a line then lines are invented'
	);
});

test('peak columns take the max of the source columns each pixel covers and never exceed full scale', () => {
	const columns = lanes.peakColumns([0, 0.2, 0.9, 0.1], 2);
	assert.deepEqual(
		columns.map((c) => c.peak),
		[0.2, 0.9],
		'if downsampling averages then transients disappear'
	);
	assert.deepEqual(lanes.peakColumns([], 100), []);
});

test('a knob value maps to a row height with 1 at the top', () => {
	assert.equal(lanes.laneY(1, 0, 56), 2);
	assert.equal(lanes.laneY(0, 56, 56), 110);
});
