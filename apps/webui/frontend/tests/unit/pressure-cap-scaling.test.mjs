/**
 * PERFMODE-04 Q29: closed-loop pressure-adaptive cap scaling.
 *
 * Must NOT be a two-state toggle: staying elevated longer must reach a
 * DIFFERENT (lower) cap than clearing sooner, and clearing must step back
 * through the same increments rather than snapping to baseline.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/pressure-cap-scaling.ts');
});

const BASELINE = 48 * 1024 * 1024;
const FLOOR = 24 * 1024 * 1024;

test('a fresh state scales to baseline exactly, unmodified', () => {
	const state = mod.createPressureCapState();
	assert.equal(mod.pressureScaledCap(BASELINE, FLOOR, state), BASELINE);
});

test('staying elevated produces at least 3 distinct, strictly decreasing caps', () => {
	let state = mod.createPressureCapState();
	const caps = [mod.pressureScaledCap(BASELINE, FLOOR, state)];
	for (let i = 0; i < mod.PRESSURE_CAP_STEPS; i += 1) {
		state = mod.stepPressureCapState(state, true);
		caps.push(mod.pressureScaledCap(BASELINE, FLOOR, state));
	}
	const distinct = new Set(caps);
	assert.ok(distinct.size >= 3, `expected >= 3 distinct caps, got ${caps.join(', ')}`);
	for (let i = 1; i < caps.length; i += 1) {
		assert.ok(caps[i] <= caps[i - 1], `cap must never increase while staying elevated: ${caps.join(', ')}`);
	}
	assert.equal(caps[caps.length - 1], FLOOR, 'after PRESSURE_CAP_STEPS elevated ticks the cap must reach the floor exactly');
});

test('elevated pressure never shrinks the cap below the floor, no matter how long it stays elevated', () => {
	let state = mod.createPressureCapState();
	for (let i = 0; i < mod.PRESSURE_CAP_STEPS + 10; i += 1) {
		state = mod.stepPressureCapState(state, true);
		assert.ok(mod.pressureScaledCap(BASELINE, FLOOR, state) >= FLOOR);
	}
	assert.equal(mod.pressureScaledCap(BASELINE, FLOOR, state), FLOOR);
});

test('clearing steps back up through the same increments, not straight to baseline', () => {
	let state = mod.createPressureCapState();
	for (let i = 0; i < mod.PRESSURE_CAP_STEPS; i += 1) state = mod.stepPressureCapState(state, true);
	assert.equal(mod.pressureScaledCap(BASELINE, FLOOR, state), FLOOR);

	state = mod.stepPressureCapState(state, false);
	const afterOneClear = mod.pressureScaledCap(BASELINE, FLOOR, state);
	assert.ok(afterOneClear > FLOOR, 'one clear tick must grow the cap');
	assert.ok(afterOneClear < BASELINE, 'one clear tick must not jump straight to baseline (not a toggle)');

	for (let i = 1; i < mod.PRESSURE_CAP_STEPS; i += 1) state = mod.stepPressureCapState(state, false);
	assert.equal(mod.pressureScaledCap(BASELINE, FLOOR, state), BASELINE, 'after enough clear ticks the cap must return exactly to baseline');
});

test('regrowth never overshoots baseline, no matter how long it stays clear', () => {
	let state = mod.createPressureCapState();
	for (let i = 0; i < mod.PRESSURE_CAP_STEPS + 10; i += 1) {
		state = mod.stepPressureCapState(state, false);
		assert.ok(mod.pressureScaledCap(BASELINE, FLOOR, state) <= BASELINE);
	}
	assert.equal(mod.pressureScaledCap(BASELINE, FLOOR, state), BASELINE);
});

test('the next cap is a function of the PREVIOUS state, not the raw elevated flag alone', () => {
	// Two different histories both currently "elevated" must NOT collapse to
	// the same next state if their step counts differ - that would be exactly
	// the two-state toggle this module exists to avoid.
	let deep = mod.createPressureCapState();
	for (let i = 0; i < 2; i += 1) deep = mod.stepPressureCapState(deep, true);
	let shallow = mod.createPressureCapState();
	shallow = mod.stepPressureCapState(shallow, true);

	assert.notEqual(
		mod.pressureScaledCap(BASELINE, FLOOR, deep),
		mod.pressureScaledCap(BASELINE, FLOOR, shallow),
		'a longer elevated history must scale to a different cap than a shorter one'
	);
});

test('an unmoved state (same object) is returned when stepping does not change steps', () => {
	// Cheap invariant, not an allocation guard: at steps=0, clearing further
	// must be a true no-op rather than silently drifting past baseline.
	const state = mod.createPressureCapState();
	const next = mod.stepPressureCapState(state, false);
	assert.equal(next, state);
});
