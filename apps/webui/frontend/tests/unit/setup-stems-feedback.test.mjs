/**
 * SETUP-15. What first-run setup says about a stems job AFTER it was enqueued.
 *
 * the maintainer, Wed 16 Sep 2026, test Mac run 2: "stems just progressed and didn't
 * give any feedback about anything happening". Measured on that machine: the
 * job WAS enqueued, and failed 198 ms later with
 *   can't open file '//scripts/stems_local_worker.py'
 * An accepted enqueue is not a started separation, so "started" is only ever
 * the first of several things this screen has to be able to say.
 *
 * - if a failed job reads as running then the one screen that could tell the
 *   tester separation never happened says it is under way -> broken.
 * - if the failure text is paraphrased then the tester cannot report it -> broken.
 * - if a job the store has not seen yet reads as failed then every enqueue
 *   flashes an error before its first update arrives -> broken.
 * - if a succeeded job still reads as running then the screen never settles -> broken.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { bundleSvelteEntry } from './mount-svelte.mjs';

// Bundled rather than imported: the module reuses errorTail and progressPct
// from the jobs store instead of keeping a second copy, and that store only
// resolves through the app's $lib alias and Svelte compiler.
let stemsJobFeedback;
before(async () => {
	({ stemsJobFeedback } = await bundleSvelteEntry(`
		export { stemsJobFeedback } from '$lib/setup/stems-feedback';
	`));
});

function job(overrides = {}) {
	return {
		id: 'a06c0a03-41c2-446e-9f6c-b2739626678f',
		kind: 'stems.separate',
		payload: { executor: 'local', scope: 'pending', tier: 'LOCAL' },
		status: 'running',
		progress: 0.25,
		message: null,
		error: null,
		...overrides
	};
}

test('no job id means nothing to report', () => {
	assert.equal(stemsJobFeedback(null, null).state, 'none');
});

test('an id the store has not seen yet is pending, never a verdict', () => {
	const f = stemsJobFeedback('a06c0a03', null);
	assert.equal(f.state, 'pending');
	assert.doesNotMatch(f.message, /fail/i);
});

test('a queued job says it is waiting to start', () => {
	const f = stemsJobFeedback('x', job({ status: 'queued', progress: 0 }));
	assert.equal(f.state, 'queued');
});

test('a running job reports its live progress as a percentage', () => {
	const f = stemsJobFeedback('x', job({ status: 'running', progress: 0.25 }));
	assert.equal(f.state, 'running');
	assert.match(f.message, /25%/);
});

test('a failed job reports failed and carries the engine error verbatim', () => {
	const err =
		"worker exited 2. stderr tail: can't open file '//scripts/stems_local_worker.py': [Errno 2] No such file or directory";
	const f = stemsJobFeedback('x', job({ status: 'failed', error: err }));
	assert.equal(f.state, 'failed');
	assert.match(f.message, /stems_local_worker\.py/);
	assert.match(f.message, /did not/i);
});

test('a failed job with no error text still reads as failed, not running', () => {
	const f = stemsJobFeedback('x', job({ status: 'failed', error: null }));
	assert.equal(f.state, 'failed');
});

test('a succeeded job reports done', () => {
	const f = stemsJobFeedback('x', job({ status: 'succeeded', progress: 1 }));
	assert.equal(f.state, 'done');
});

test('a cancelled job reports cancelled, not done and not failed', () => {
	const f = stemsJobFeedback('x', job({ status: 'cancelled' }));
	assert.equal(f.state, 'cancelled');
});

test('a status outside the engine contract reads as unknown, never as a guess', () => {
	const f = stemsJobFeedback('x', job({ status: 'exploded' }));
	assert.equal(f.state, 'unknown');
	assert.match(f.message, /exploded/);
});
