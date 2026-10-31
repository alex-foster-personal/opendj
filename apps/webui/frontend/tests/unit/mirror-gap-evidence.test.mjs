/**
 * A mirror stall names what the page saw during the gap (issue #5887).
 *
 * `mirror-stall` fired for a blocked main thread and for a throttled or
 * starved WebContent process alike, so 25 s freezes on silver could not be
 * told apart. The stall row now carries how long the page was hidden and how
 * much long-task time the main thread logged in that gap.
 *
 * [if] the page was hidden for part of the gap [then] that part is reported
 * [if] long tasks ran in the gap [then] their total is reported
 * [if] the engine has no long-task observer [then] it says unmeasured, never 0
 * [if] a window is taken [then] the next one starts from zero
 * [if] a long task's entry is still queued when the window is taken [then] the
 *   take drains it and counts it in the window it ran in, not the next one
 * [if] a long task straddles two windows [then] each gets its own part
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/mirror-publish-stall.ts');
});

function fakeEnv({ hidden = false, longTasks = true } = {}) {
	const env = {
		t: 0,
		hidden,
		visibility: null,
		longTask: null,
		now: () => env.t,
		isHidden: () => env.hidden,
		onVisibilityChange: (listener) => {
			env.visibility = listener;
			return () => {
				env.visibility = null;
			};
		},
		queued: [],
		flushes: 0,
		observeLongTasks: (listener) => {
			if (!longTasks) return null;
			env.longTask = listener;
			return {
				flush: () => {
					env.flushes += 1;
					for (const task of env.queued.splice(0)) listener(task);
				},
				stop: () => {
					env.longTask = null;
				}
			};
		},
		/** A long task the engine has recorded but not yet delivered to the callback. */
		queueLongTask(startMs, durationMs) {
			env.queued.push({ startMs, durationMs });
		},
		setHidden(value) {
			env.hidden = value;
			env.visibility?.();
		}
	};
	return env;
}

test('hidden time and long tasks inside the gap are reported', () => {
	const env = fakeEnv();
	const tracker = mod.createMirrorGapTracker(env);
	env.t = 1_000;
	env.setHidden(true);
	env.t = 21_000;
	env.setHidden(false);
	env.longTask({ startMs: 22_000, durationMs: 120 });
	env.longTask({ startMs: 23_000, durationMs: 80 });
	env.t = 25_462;
	const evidence = tracker.take();
	assert.deepEqual(evidence, { hiddenMs: 20_000, longTaskMs: 200 });
	assert.equal(
		mod.mirrorStallMessage(25_462, evidence),
		'ui-mirror publish missed for 25462ms; hidden 20000ms of it; long tasks 200ms'
	);
});

test('a page still hidden at the take counts up to now, and the next window restarts', () => {
	const env = fakeEnv({ hidden: true });
	const tracker = mod.createMirrorGapTracker(env);
	env.t = 6_000;
	assert.equal(tracker.take().hiddenMs, 6_000);
	env.t = 9_000;
	const next = tracker.take();
	assert.equal(next.hiddenMs, 3_000, 'the second window re-counted the first');
	assert.equal(next.longTaskMs, 0);
});

test('no long-task observer reads as unmeasured, never as zero', () => {
	const env = fakeEnv({ longTasks: false });
	const tracker = mod.createMirrorGapTracker(env);
	env.t = 7_000;
	const evidence = tracker.take();
	assert.equal(evidence.longTaskMs, null);
	assert.match(mod.mirrorStallMessage(7_000, evidence), /long tasks unmeasured$/);
});

test('CONTROL: a visible, idle gap reports zeros, and the old message is unchanged without evidence', () => {
	const env = fakeEnv();
	const tracker = mod.createMirrorGapTracker(env);
	env.t = 6_000;
	assert.deepEqual(tracker.take(), { hiddenMs: 0, longTaskMs: 0 });
	assert.equal(mod.mirrorStallMessage(6_000), 'ui-mirror publish missed for 6000ms');
});

test('dispose stops both subscriptions', () => {
	const env = fakeEnv();
	const tracker = mod.createMirrorGapTracker(env);
	tracker.dispose();
	assert.equal(env.visibility, null);
	assert.equal(env.longTask, null);
});

test('a long task still queued at the take is drained and counted in the window it ran in', () => {
	const env = fakeEnv();
	const tracker = mod.createMirrorGapTracker(env);
	// The main thread blocked from 1 s to 7 s; the overdue publish runs before
	// the observer callback has been delivered.
	env.queueLongTask(1_000, 6_000);
	env.t = 7_010;
	const stalled = tracker.take();
	assert.equal(env.flushes, 1, 'take did not drain the queued entries');
	assert.equal(stalled.longTaskMs, 6_000, 'the block was missing from the stalled window');
	env.t = 8_010;
	assert.equal(tracker.take().longTaskMs, 0, 'the block leaked into the next, healthy window');
});

test('a long task delivered late is still counted only for the time inside each window', () => {
	const env = fakeEnv();
	const tracker = mod.createMirrorGapTracker(env);
	env.t = 5_000;
	// Delivered before this take, but it started in the window and runs past it.
	env.longTask({ startMs: 4_000, durationMs: 1_500 });
	assert.equal(tracker.take().longTaskMs, 1_000, 'the part after the take was counted early');
	env.t = 6_000;
	assert.equal(tracker.take().longTaskMs, 500, 'the remainder was lost');
	env.t = 7_000;
	assert.equal(tracker.take().longTaskMs, 0, 'a finished task was counted twice');
});

test('finished long tasks on a tab that never publishes are totalled, not held', () => {
	// Sol P2 on #8045: every delivered task used to wait in a list until the
	// next take, so a tab that stopped publishing grew it without bound. A task
	// that has ended when delivered is folded into the window's total at once.
	const env = fakeEnv();
	const tracker = mod.createMirrorGapTracker(env);
	for (let i = 0; i < 1_000; i += 1) {
		env.t = i * 100 + 60;
		env.longTask({ startMs: i * 100, durationMs: 50 });
	}
	env.t = 200_000;
	assert.equal(tracker.take().longTaskMs, 50_000, 'finished tasks were lost or miscounted');
	env.t = 201_000;
	assert.equal(tracker.take().longTaskMs, 0, 'the folded total leaked into the next window');
});
