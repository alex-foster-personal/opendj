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

import {
	watchContinuousPlaybackUntil,
	watchGigDecksPlayingUntil
} from '../../scripts/perf/trackify-playback-watch.mjs';

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

test('Gig: all four decks stay playing at every poll then the watch resolves without throwing', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		const page = fakePage([true, true, true]);
		const signal = gate();
		const done = watchGigDecksPlayingUntil(page, signal.promise, [1, 2, 3, 4], { pollMs: 1_000 });
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

test('Gig: one deck stops mid-window (e.g. its track ended) and recovers before the signal fires then the watch still rejects, naming all four decks', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		// The bug this guards against: GIG_READY only proves the four decks
		// were playing before the wait started -- a deck whose short track
		// ends mid-window (then perhaps loops or gets replaced) would sample
		// as a valid four-deck steady state under an endpoint-only check.
		const page = fakePage([true, false, true]);
		const signal = gate();
		const done = watchGigDecksPlayingUntil(page, signal.promise, [1, 2, 3, 4], { pollMs: 1_000 });
		await settle();
		mock.timers.tick(1_000);
		await settle();
		mock.timers.tick(1_000);
		await settle();
		signal.release();
		await assert.rejects(done, /Gig was not continuously playing on all four decks/);
	} finally {
		mock.timers.reset();
	}
});

test('Gig control: a page that never has all four decks playing also rejects', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		const page = fakePage([false, false]);
		const signal = gate();
		const done = watchGigDecksPlayingUntil(page, signal.promise, [1, 2, 3, 4], { pollMs: 1_000 });
		await settle();
		mock.timers.tick(1_000);
		await settle();
		signal.release();
		await assert.rejects(done, /Gig was not continuously playing on all four decks/);
	} finally {
		mock.timers.reset();
	}
});

test('Gig: the deck id list is threaded through to each page.evaluate call', async () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	try {
		const seenArgs = [];
		const page = {
			evaluate: async (_fn, arg) => {
				seenArgs.push(arg);
				return true;
			}
		};
		const signal = gate();
		const done = watchGigDecksPlayingUntil(page, signal.promise, [1, 2, 3, 4], { pollMs: 1_000 });
		await settle();
		signal.release();
		await done;
		assert.ok(seenArgs.length > 0, 'evaluate must have been called at least once');
		for (const arg of seenArgs) {
			assert.deepEqual(arg, [1, 2, 3, 4]);
		}
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
