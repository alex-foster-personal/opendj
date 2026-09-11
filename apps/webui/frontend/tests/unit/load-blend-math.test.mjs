/**
 * Pure Load-to-CH intro-blend axis mapping (issue #286).
 *
 * Real numbers, no fake mixer.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let loadBlendProgress;
let loadBlendScrubMs;
let loadBlendEqAt;
let LOAD_BLEND_INCOMING_START;
let LOAD_BLEND_MASTER_START;
let LOAD_BLEND_FLAT;
let FADER_PX;
let T_OVERSHOOT;
let SCRUB_MS_PER_PX;

before(async () => {
	({
		loadBlendProgress,
		loadBlendScrubMs,
		loadBlendEqAt,
		LOAD_BLEND_INCOMING_START,
		LOAD_BLEND_MASTER_START,
		LOAD_BLEND_FLAT,
		FADER_PX,
		T_OVERSHOOT,
		SCRUB_MS_PER_PX
	} = await loadTypeScriptModule('src/lib/rb/load-blend-math.ts'));
});

function assertClose(actual, expected, label) {
	assert.ok(
		Math.abs(actual - expected) < 1e-9,
		`${label}: expected ${expected}, got ${actual}`
	);
}

test('dy = 0 keeps fader 0, t 0, and EQ at the intro table', () => {
	const progress = loadBlendProgress({ dyPx: 0 });
	assert.equal(progress.fader, 0);
	assert.equal(progress.t, 0);
	const eq = loadBlendEqAt(0);
	assert.equal(eq.incoming.low, LOAD_BLEND_INCOMING_START.low);
	assert.equal(eq.incoming.mid, LOAD_BLEND_INCOMING_START.mid);
	assert.equal(eq.incoming.filter, LOAD_BLEND_INCOMING_START.filter);
	assert.equal(eq.master.low, LOAD_BLEND_MASTER_START.low);
	assert.equal(eq.master.mid, LOAD_BLEND_MASTER_START.mid);
});

test('dy = FADER_PX raises engine fader to 1 with t still below 1', () => {
	const progress = loadBlendProgress({ dyPx: FADER_PX });
	assert.equal(progress.fader, 1);
	assertClose(progress.t, 1 / T_OVERSHOOT, 't at visual 1.0');
	const eq = loadBlendEqAt(progress.t);
	assert.ok(eq.incoming.low > LOAD_BLEND_INCOMING_START.low);
	assert.ok(eq.incoming.low < LOAD_BLEND_FLAT);
	assert.ok(eq.incoming.mid > LOAD_BLEND_INCOMING_START.mid);
	assert.ok(eq.incoming.mid < LOAD_BLEND_FLAT);
	assert.ok(eq.incoming.filter < LOAD_BLEND_INCOMING_START.filter);
	assert.ok(eq.incoming.filter > LOAD_BLEND_FLAT);
	assert.ok(eq.master.low < LOAD_BLEND_MASTER_START.low);
	assert.ok(eq.master.low > LOAD_BLEND_FLAT);
});

test('dy = FADER_PX * T_OVERSHOOT clamps fader at 1 and flats EQ/filter', () => {
	const progress = loadBlendProgress({ dyPx: FADER_PX * T_OVERSHOOT });
	assert.equal(progress.fader, 1);
	assert.equal(progress.t, 1);
	const eq = loadBlendEqAt(1);
	assert.equal(eq.incoming.low, LOAD_BLEND_FLAT);
	assert.equal(eq.incoming.mid, LOAD_BLEND_FLAT);
	assert.equal(eq.incoming.filter, LOAD_BLEND_FLAT);
	assert.equal(eq.master.low, LOAD_BLEND_FLAT);
	assert.equal(eq.master.mid, LOAD_BLEND_FLAT);
});

test('negative dy does not duck the master: fader 0, t 0', () => {
	const progress = loadBlendProgress({ dyPx: -40 });
	assert.equal(progress.fader, 0);
	assert.equal(progress.t, 0);
});

test('horizontal +250 px scrubs +8000 ms and clamps to duration', () => {
	assert.equal(SCRUB_MS_PER_PX, 32);
	assert.equal(
		loadBlendScrubMs({ dxPx: 250, originMs: 0, durationMs: 60000 }),
		8000
	);
	assert.equal(
		loadBlendScrubMs({ dxPx: 250, originMs: 0, durationMs: 1000 }),
		1000
	);
	assert.equal(
		loadBlendScrubMs({ dxPx: -10, originMs: 0, durationMs: 60000 }),
		0
	);
});
