import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * stageTimer is the one stage stopwatch the lazy stem upgrade records its
 * probe, fetch, decode and processor-create times with (PERFMODE-15 moved it
 * out of the engine).
 *
 * The clock is injected, as the other perf timers' is, not a real sleep: a real
 * 20 ms timer read against a >= 15 ms floor measured 13 ms on megamac-vm-4
 * (Mon 28 Sep 2026, CI job 109093105487), because a slow host's gap between the
 * clock read and the timer start comes off the reading. With a scripted `now`
 * every recorded ms is exact, and nothing global is replaced.
 *
 * Regression lines:
 * - if stageTimer stops passing the awaited value through then every timed
 *   stage of the upgrade returns undefined
 * - if stageTimer records anything but the rounded clock difference then a
 *   stage's ms is wrong (floor, ceil or no rounding each fail below)
 * - if stageTimer stops recording a stage that throws then a failed stem fetch
 *   loses the only timing that says where the upgrade died
 * - if stageTimer reads the clock other than once at each end then a stage's ms
 *   spans the wrong interval
 * - if the default clock stops yielding whole, non-negative ms then production
 *   stages (which never pass a clock) record garbage
 */

const { stageTimer } = await loadTypeScriptModule('src/lib/rb/perf-event-log.ts');

/** A clock that returns `readings` in order and counts its reads. */
function scriptedClock(readings) {
	const queue = [...readings];
	const clock = () => {
		assert.ok(queue.length > 0, `the clock was read more than the ${readings.length} scripted times`);
		clock.reads += 1;
		return queue.shift();
	};
	clock.reads = 0;
	return clock;
}

test('stageTimer passes the value through and records the stage in whole ms', async () => {
	// 20.4 -> 20 and 20.6 -> 21: only Math.round gets both (floor, ceil and trunc each miss one).
	const now = scriptedClock([1000, 1020.4, 2000, 2020.6]);
	const stages = {};
	const time = stageTimer(stages, now);

	assert.equal(await time('probeStem', Promise.resolve('ok')), 'ok');
	assert.equal(await time('fetchStems', Promise.resolve(42)), 42);

	assert.equal(stages.probeStem, 20);
	assert.equal(stages.fetchStems, 21);
	assert.equal(now.reads, 4, 'each stage reads the clock exactly twice');
});

test('stageTimer records a stage that throws, then rethrows', async () => {
	const stages = {};
	const time = stageTimer(stages, scriptedClock([5000, 5007.2]));
	await assert.rejects(time('fetchStems', Promise.reject(new Error('fetch failed'))), /fetch failed/);
	assert.equal(stages.fetchStems, 7, 'a failed stage must still be timed');
});

test('stageTimer on its default clock records a whole, non-negative ms', async () => {
	// The production path: no clock passed. No duration bound, so no host can flake it.
	const stages = {};
	const time = stageTimer(stages);
	assert.equal(await time('decode', Promise.resolve('pcm')), 'pcm');
	assert.ok(Number.isInteger(stages.decode) && stages.decode >= 0, `got ${stages.decode}`);
});
