/**
 * SETUP-14. What the wizard's last screen says about analysis after a FOLDER
 * import.
 *
 * the maintainer, Wed 16 Sep 2026, after run 2 on the test Mac: "doesn't know to use our
 * own beat grid analysis when not importing from rbx ... just making sure it
 * at least uses it when it HAS to would be great."
 *
 * A folder import is the only path with no rekordbox ANLZ to read, so it is
 * the path where own analysis is not a preference but the only source there
 * is. The screen shipped before this said the tracks have "no BPM, no key and
 * no beatgrid ... and none are guessed" and stopped there, which reads as a
 * dead end even when the engine is about to analyze every one of them.
 *
 * - if a queue with work pending reads as nothing-to-do then the screen tells
 *   the user their library is finished when it has not started -> broken.
 * - if a drain that FAILED reads as work in progress then a build that cannot
 *   analyze at all shows a progress line forever -> broken, and it is the
 *   exact state measured on the shipped build.
 * - if an empty queue reads as work in progress then a rekordbox import, or a
 *   second visit to this screen, shows a bar that never moves -> broken.
 * - if the counts come from the import record rather than the live queue then
 *   the number stops moving the moment the screen renders -> broken.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Bundled through esbuild like every other .ts import under tests/unit: the
// pinned CI node (22.14.0) has no type stripping, so a bare .ts import throws
// ERR_UNKNOWN_FILE_EXTENSION there and only passed on runners whose host node
// shadowed the pinned one.
const { analysisFollowup } = await loadTypeScriptModule('src/lib/setup/analysis-followup.ts');

function queue(overrides = {}) {
	return {
		unmapped: 100,
		pending: 100,
		analyzed: 0,
		unreachable: 0,
		signature: 'abc',
		items: [],
		job: {
			running: false,
			phase: 'idle',
			steps: [],
			current_step: null,
			step_done: 0,
			step_total: 0,
			steps_completed: [],
			started_at: null,
			finished_at: null,
			error: null,
			log_tail: [],
			recently_done_ids: []
		},
		auto: {
			enabled: true,
			interval_s: 60,
			attempts: 0,
			last_started_at: null,
			last_signature: null,
			last_outcome: null
		},
		...overrides
	};
}

test('a queue not read yet is unknown, never a verdict', () => {
	assert.equal(analysisFollowup(null).state, 'unknown');
});

test('tracks waiting to be analyzed report working, with live counts', () => {
	const f = analysisFollowup(queue());
	assert.equal(f.state, 'working');
	assert.equal(f.analyzed, 0);
	assert.equal(f.total, 100);
	assert.match(f.message, /BPM/);
	assert.match(f.message, /0 of 100/);
});

test('progress is read from the live queue, not frozen at the import', () => {
	const f = analysisFollowup(queue({ pending: 63, analyzed: 37 }));
	assert.equal(f.state, 'working');
	assert.equal(f.analyzed, 37);
	assert.equal(f.total, 100);
	assert.match(f.message, /37 of 100/);
});

test('an emptied queue that analyzed something reports done', () => {
	const f = analysisFollowup(queue({ pending: 0, analyzed: 100 }));
	assert.equal(f.state, 'done');
	assert.equal(f.analyzed, 100);
	assert.match(f.message, /100/);
});

test('a failed drain reports failed and carries the engine error verbatim', () => {
	const f = analysisFollowup(
		queue({ job: { ...queue().job, phase: 'error', error: 'BackendNotAvailable: librosa' } })
	);
	assert.equal(f.state, 'failed');
	assert.match(f.message, /BackendNotAvailable: librosa/);
});

test('a failed drain stays failed even while tracks are still pending', () => {
	// The overshoot control: "pending > 0 means working" would swallow the one
	// state the user most needs to see, because a failed drain leaves every
	// track pending by definition.
	const f = analysisFollowup(
		queue({ pending: 100, job: { ...queue().job, phase: 'error', error: 'boom' } })
	);
	assert.equal(f.state, 'failed');
});

test('nothing unmapped reports not-needed, not done and not working', () => {
	const f = analysisFollowup(queue({ unmapped: 0, pending: 0, analyzed: 0 }));
	assert.equal(f.state, 'not-needed');
});

test('tracks with no reachable audio are named separately, never folded into done', () => {
	const f = analysisFollowup(queue({ pending: 0, analyzed: 90, unreachable: 10 }));
	assert.equal(f.state, 'done');
	assert.equal(f.unreachable, 10);
	assert.match(f.message, /10/);
});

test('shouldStartDrain is true exactly when there is work and no job running', () => {
	const { shouldStartDrain } = analysisFollowup(queue());
	assert.equal(shouldStartDrain, true);
});

test('shouldStartDrain is false while a drain is already running', () => {
	const f = analysisFollowup(queue({ job: { ...queue().job, running: true, phase: 'running' } }));
	assert.equal(f.shouldStartDrain, false);
	assert.equal(f.state, 'working');
});

test('shouldStartDrain is false after a drain failed, so the wizard cannot loop', () => {
	const f = analysisFollowup(
		queue({ job: { ...queue().job, phase: 'error', error: 'boom' } })
	);
	assert.equal(f.shouldStartDrain, false);
});

test('shouldStartDrain is false when there is nothing to drain', () => {
	assert.equal(analysisFollowup(queue({ unmapped: 0, pending: 0 })).shouldStartDrain, false);
	assert.equal(analysisFollowup(null).shouldStartDrain, false);
});
