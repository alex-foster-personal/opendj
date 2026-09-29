import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * stageTimer is the one stage stopwatch the lazy stem upgrade records its
 * probe, fetch, decode and processor-create times with (PERFMODE-15 moved it
 * out of the engine).
 *
 * The clock is scripted, not real: a real 20 ms timer read against a >= 15 ms
 * floor measured 13 ms on megamac-vm-4 (Mon 28 Sep 2026, CI job 109093105487),
 * because a slow host's gap between the clock read and the timer start comes
 * off the reading. With `performance.now` scripted, every recorded ms is exact.
 *
 * Regression lines:
 * - if stageTimer stops passing the awaited value through then every timed
 *   stage of the upgrade returns undefined
 * - if stageTimer records anything but the rounded clock difference then a
 *   stage's ms is wrong (floor, ceil or no rounding each fail below)
 * - if stageTimer stops recording a stage that throws then a failed stem fetch
 *   loses the only timing that says where the upgrade died
 * - if stageTimer stops reading performance.now then its ms is not the clock
 *   the rest of the perf log uses
 */

const { stageTimer } = await loadTypeScriptModule('src/lib/rb/perf-event-log.ts');

/** Script `performance.now` to return `readings` in order; restored after the test. */
function scriptClock(t, readings) {
	const queue = [...readings];
	return t.mock.method(globalThis.performance, 'now', () => {
		assert.ok(queue.length > 0, `performance.now read more than the ${readings.length} scripted times`);
		return queue.shift();
	});
}

test('stageTimer passes the value through and records the stage in whole ms', async (t) => {
	// 20.4 -> 20 and 20.6 -> 21: only Math.round gets both (floor, ceil and trunc each miss one).
	const now = scriptClock(t, [1000, 1020.4, 2000, 2020.6]);
	const stages = {};
	const time = stageTimer(stages);

	assert.equal(await time('probeStem', Promise.resolve('ok')), 'ok');
	assert.equal(await time('fetchStems', Promise.resolve(42)), 42);

	assert.equal(stages.probeStem, 20);
	assert.equal(stages.fetchStems, 21);
	assert.equal(now.mock.callCount(), 4, 'each stage reads the clock exactly twice');
});

test('stageTimer records a stage that throws, then rethrows', async (t) => {
	scriptClock(t, [5000, 5007.2]);
	const stages = {};
	const time = stageTimer(stages);
	await assert.rejects(time('fetchStems', Promise.reject(new Error('fetch failed'))), /fetch failed/);
	assert.equal(stages.fetchStems, 7, 'a failed stage must still be timed');
});
