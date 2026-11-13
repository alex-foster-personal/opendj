/**
 * Issue #13335: incoming low-kill timeline at the bass-hit boundary (no audio).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let INCOMING_LOW_KILL_KNOB;
let INCOMING_LOW_RELEASE_KNOB;
let incomingLowKnobAtMs;
let resolveBassHitBoundaryMs;
let DEFAULT_AUTO_PLAY_NEXT_CONFIG;

const BOUNDARY_MS = 2_000;

before(async () => {
	({
		INCOMING_LOW_KILL_KNOB,
		INCOMING_LOW_RELEASE_KNOB,
		incomingLowKnobAtMs,
		resolveBassHitBoundaryMs,
		DEFAULT_AUTO_PLAY_NEXT_CONFIG
	} = await loadTypeScriptModule('src/lib/rb/transition-score.ts'));
});

test('[if] incoming is before the bass-hit boundary [then] low stays killed, [else stop]', () => {
	assert.equal(incomingLowKnobAtMs(1_999, BOUNDARY_MS), INCOMING_LOW_KILL_KNOB);
	assert.equal(incomingLowKnobAtMs(BOUNDARY_MS - 1, BOUNDARY_MS), INCOMING_LOW_KILL_KNOB);
	assert.equal(INCOMING_LOW_KILL_KNOB, 0);
});

test('[if] the boundary is reached exactly [then] low releases, [else stop]', () => {
	assert.equal(incomingLowKnobAtMs(BOUNDARY_MS, BOUNDARY_MS), INCOMING_LOW_RELEASE_KNOB);
	assert.equal(INCOMING_LOW_RELEASE_KNOB, 0.5);
});

test('[if] the bass-hit boundary is unavailable [then] low stays killed, [else stop]', () => {
	assert.equal(incomingLowKnobAtMs(0, null), INCOMING_LOW_KILL_KNOB);
	assert.equal(incomingLowKnobAtMs(60_000, null), INCOMING_LOW_KILL_KNOB);
});

test('incoming low releases strictly after the bass-hit boundary', () => {
	assert.equal(incomingLowKnobAtMs(BOUNDARY_MS + 1, BOUNDARY_MS), INCOMING_LOW_RELEASE_KNOB);
});

test('resolveBassHitBoundaryMs feeds kill-before and release-at policy', () => {
	const length = 1000;
	const waveform = {
		kind: 'tri',
		preview: { length: 1, low: [0], mid: [0], high: [0] },
		detail: {
			length,
			low: new Array(length).fill(0).map((_, i) => (i < 500 ? 0.1 : 0.9)),
			mid: new Array(length).fill(0.3),
			high: new Array(length).fill(0.2)
		}
	};
	const beats = [];
	for (let i = 0; i < 16; i++) beats.push({ n: (i % 4) + 1, bpm: 128, t: i * 0.469 });
	const durationSec = 10;
	const boundary = resolveBassHitBoundaryMs(
		waveform,
		beats,
		durationSec,
		0,
		DEFAULT_AUTO_PLAY_NEXT_CONFIG
	);
	assert.ok(
		boundary !== null && Math.abs(boundary - 5000) < 50,
		`expected ~5000ms, got ${boundary}`
	);
	assert.equal(incomingLowKnobAtMs(boundary - 1, boundary), INCOMING_LOW_KILL_KNOB);
	assert.equal(incomingLowKnobAtMs(boundary, boundary), INCOMING_LOW_RELEASE_KNOB);
});

test('[if] positionMs is non-finite [then] incomingLowKnobAtMs throws, [else stop]', () => {
	assert.throws(() => incomingLowKnobAtMs(Number.NaN, BOUNDARY_MS), RangeError);
});
