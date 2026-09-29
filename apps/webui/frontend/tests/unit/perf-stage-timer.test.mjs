import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * stageTimer is the one stage stopwatch the lazy stem upgrade records its
 * probe, fetch, decode and processor-create times with (PERFMODE-15 moved it
 * out of the engine).
 *
 * Regression lines:
 * - if stageTimer stops recording a stage that throws then a failed stem fetch
 *   loses the only timing that says where the upgrade died
 * - if stageTimer stops passing the awaited value through then every timed
 *   stage of the upgrade returns undefined
 */

const { stageTimer } = await loadTypeScriptModule('src/lib/rb/perf-event-log.ts');

test('stageTimer passes the value through and records the stage in whole ms', async () => {
	const stages = {};
	const time = stageTimer(stages);
	// The 20 ms sleep starts in a microtask, so after stageTimer has read its clock.
	// Built directly as the argument, its timer started BEFORE the clock, and a slow
	// host's gap between the two came off the reading ("got 14" on megamac-vm-4).
	const sleepStartedAfterTheClock = Promise.resolve().then(
		() => new Promise((resolve) => setTimeout(() => resolve('ok'), 20))
	);
	const value = await time('probeStem', sleepStartedAfterTheClock);
	assert.equal(value, 'ok');
	assert.ok(Number.isInteger(stages.probeStem), `expected whole ms, got ${stages.probeStem}`);
	assert.ok(stages.probeStem >= 15, `expected about 20 ms, got ${stages.probeStem}`);
});

test('stageTimer records a stage that throws, then rethrows', async () => {
	const stages = {};
	const time = stageTimer(stages);
	await assert.rejects(time('fetchStems', Promise.reject(new Error('fetch failed'))), /fetch failed/);
	assert.equal(typeof stages.fetchStems, 'number', 'a failed stage must still be timed');
});
