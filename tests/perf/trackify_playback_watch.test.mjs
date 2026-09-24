/**
 * Sol review, PR #3676: `mode_ratio_browser.mjs` checked Trackify playback
 * only immediately before printing TRACKIFY_READY and again right before
 * DONE, never during the measured window in between (where
 * `capture_mode_ratios.py`'s process-tree sampler actually runs). A page
 * that stalls for most of that window and resumes just before the final
 * check would still report `measured: true` against an invalid workload.
 *
 * `watchContinuousPlaybackUntil` is kept side-effect-free (no Playwright
 * launch, no argv parsing) specifically so it is importable here against a
 * fake `page`, unlike `mode_ratio_browser.mjs` itself.
 */
import assert from 'node:assert/strict';
import { mock, test } from 'node:test';

import { watchContinuousPlaybackUntil } from '../../scripts/perf/trackify-playback-watch.mjs';

/** Drains the microtask queue; setImmediate is never in the mocked timer APIs. */
const settle = () => new Promise((resolve) => setImmediate(resolve));

function gate() {
	let release = () => {};
	const promise = new Promise((resolve) => {
		release = resolve;
	});
	return { promise, release };
}

/** A fake Playwright `page`: `evaluate` returns the next value of a scripted
 * playing/not-playing sequence on each poll, holding the last value once the
 * sequence is exhausted. */
function fakePage(playingSequence) {
	let index = 0;
	return {
		evaluate: async () => {
			const value = playingSequence[Math.min(index, playingSequence.length - 1)];
			index += 1;
			return value;
		}
	};
}

test('if playback stays true at every poll then the watch resolves without throwing', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		const page = fakePage([true, true, true]);
		const signal = gate();
		const done = watchContinuousPlaybackUntil(page, signal.promise, { pollMs: 1_000 });
		await settle();
		mock.timers.tick(1_000);
		await settle();
		mock.timers.tick(1_000);
		await settle();
		signal.release();
		await assert.doesNotReject(done);
	} finally {
		mock.timers.reset();
	}
});

test('if playback drops mid-window and recovers before the signal fires then the watch still rejects', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		// playing, then a gap, then playing again by the time the signal
		// arrives -- the bug this guards against: an endpoint-only check
		// would see "playing" the moment the signal fires and miss the gap
		// that happened for the rest of the window.
		const page = fakePage([true, false, true]);
		const signal = gate();
		const done = watchContinuousPlaybackUntil(page, signal.promise, { pollMs: 1_000 });
		await settle();
		mock.timers.tick(1_000);
		await settle();
		mock.timers.tick(1_000);
		await settle();
		signal.release();
		await assert.rejects(done, /not continuously playing/);
	} finally {
		mock.timers.reset();
	}
});

test('control: a page that never plays at all also rejects (not just the mid-window-drop shape)', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		const page = fakePage([false, false]);
		const signal = gate();
		const done = watchContinuousPlaybackUntil(page, signal.promise, { pollMs: 1_000 });
		await settle();
		mock.timers.tick(1_000);
		await settle();
		signal.release();
		await assert.rejects(done, /not continuously playing/);
	} finally {
		mock.timers.reset();
	}
});

test('a page.evaluate rejection (e.g. the page navigated away) counts as a gap, not a silent pass', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		let calls = 0;
		const page = {
			evaluate: async () => {
				calls += 1;
				if (calls === 1) return true;
				throw new Error('page has been closed');
			}
		};
		const signal = gate();
		const done = watchContinuousPlaybackUntil(page, signal.promise, { pollMs: 1_000 });
		await settle();
		mock.timers.tick(1_000);
		await settle();
		signal.release();
		await assert.rejects(done, /not continuously playing/);
	} finally {
		mock.timers.reset();
	}
});
