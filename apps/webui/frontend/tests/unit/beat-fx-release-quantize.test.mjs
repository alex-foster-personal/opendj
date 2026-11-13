// requirement: BEATFX-10766
// requirement: BEATFX-10766b
// Pure-seam scheduling tests for beat FX quantize and Release FX timing (#13388), spec
// (private path) sections 1b and 2. No AudioContext and no render:
// the render checks live in beat-fx-quantize.test.mjs.
// [if] quantize is on before a grid boundary and the plan is not state on at that boundary [then] broken
// [if] quantize is off and the plan is not state off at once [then] broken
// [if] a release held across a boundary does not wait for the following boundary or rewinds the press step [then] broken
// [if] non-finite beat timing is accepted instead of refused with a RangeError [then] broken
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const BEAT_SEC = 60 / 124;
const GRID = Array.from({ length: 32 }, (_, k) => 0.1 + k * BEAT_SEC);
const OFF_BEAT_NOW = GRID[3] + 0.37 * BEAT_SEC;
const NON_FINITE = [NaN, Infinity, -Infinity];

const { planQuantizedTransition, scheduleGainStep, BEAT_FX_RAMP_SEC } = await loadTypeScriptModule(
	'src/lib/rb/beat-fx-quantize.ts'
);

//----- helpers

/** Record AudioParam automation calls so a scheduled step can be read back without a context. */
class RecordingParam {
	constructor(value) {
		this.value = value;
		this.events = [];
	}
	setValueAtTime(v, t) {
		this.events.push({ kind: 'set', v, t });
	}
	linearRampToValueAtTime(v, t) {
		this.events.push({ kind: 'linear', v, t });
	}
	cancelScheduledValues(t) {
		this.events = this.events.filter((e) => e.t < t);
	}
}

const plan = (quantize, nowSec, beats = 1, gridBeatsSec = GRID) =>
	planQuantizedTransition({ quantize, gridBeatsSec, nowSec, beats });

//----- tests

describe('beat FX release quantize scheduling (pure seam)', () => {
	// [if] quantize is on and now sits before a boundary [then] state is on at the next min(division, 1 beat) boundary, [else stop].
	test('quantize on before a boundary lands on the next min(division, 1 beat) boundary', () => {
		for (const beats of [1 / 4, 1 / 2, 1, 4]) {
			const step = Math.min(beats, 1) * BEAT_SEC;
			const k = Math.ceil((OFF_BEAT_NOW - GRID[3]) / step);
			const got = plan(true, OFF_BEAT_NOW, beats);
			assert.equal(got.state, 'on', `[if] beats ${beats} plan is ${got.state} [then] broken`);
			assert.ok(
				Math.abs(got.atSec - (GRID[3] + k * step)) < 1e-9,
				`[if] beats ${beats} lands at ${got.atSec}, not the next boundary [then] broken`
			);
			assert.ok(got.atSec > OFF_BEAT_NOW && got.atSec - OFF_BEAT_NOW <= BEAT_SEC + 1e-9);
		}
	});

	// [if] quantize is off [then] the plan is state off at now, whatever the grid, [else stop].
	test('quantize off plans off at once, ignoring the grid', () => {
		for (const grid of [GRID, []]) {
			assert.deepEqual(plan(false, OFF_BEAT_NOW, 1, grid), { state: 'off', atSec: OFF_BEAT_NOW });
		}
	});

	// [if] a release is held past the press boundary [then] the release waits for the following boundary and keeps the press step, [else stop].
	test('release held across a boundary waits for the following boundary', () => {
		const dry = new RecordingParam(1);
		const press = plan(true, OFF_BEAT_NOW, 1 / 2);
		scheduleGainStep(dry, 0, press.atSec);
		const release = plan(true, press.atSec + 0.01, 1 / 2);
		assert.equal(release.state, 'on');
		assert.ok(
			Math.abs(release.atSec - (press.atSec + BEAT_SEC / 2)) < 1e-9,
			`[if] release lands at ${release.atSec}, not the boundary after the press [then] broken`
		);
		scheduleGainStep(dry, 1, release.atSec);
		assert.deepEqual(dry.events, [
			{ kind: 'set', v: 1, t: press.atSec },
			{ kind: 'linear', v: 0, t: press.atSec + BEAT_FX_RAMP_SEC },
			{ kind: 'set', v: 0, t: release.atSec },
			{ kind: 'linear', v: 1, t: release.atSec + BEAT_FX_RAMP_SEC }
		]);
	});

	// [if] nowSec is NaN or +-Infinity with quantize on [then] planning throws RangeError naming nowSec, [else stop].
	test('non-finite nowSec is refused with quantize on', () => {
		for (const nowSec of NON_FINITE) {
			assert.throws(
				() => plan(true, nowSec),
				(e) => e instanceof RangeError && /nowSec/.test(e.message)
			);
		}
	});
});
