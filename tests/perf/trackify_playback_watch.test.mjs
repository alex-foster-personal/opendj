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
	gigBaselineDeckFaults,
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

test('a brief mid-window gap that recovers within the handoff tolerance does not invalidate a Trackify capture (Sol review, PR #3676)', async () => {
	mock.timers.enable({ apis: ['setTimeout', 'Date'] });
	try {
		// Trackify's own normal operation: unload, load, then play at every
		// track boundary, so `deck.playing` legitimately reads false for a
		// moment. A single poll landing in that gap, followed by a recovery
		// on the very next poll, must not invalidate an otherwise healthy
		// capture (unlike the old zero-tolerance behavior this replaces).
		const page = fakePage([true, false, true]);
		const signal = gate();
		const done = watchContinuousPlaybackUntil(page, signal.promise, {
			pollMs: 1_000,
			tolerateGapsUnderMs: 3_000
		});
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

test('a sustained mid-window gap beyond the handoff tolerance still invalidates a Trackify capture, even if it later recovers (Sol review, PR #3676)', async () => {
	mock.timers.enable({ apis: ['setTimeout', 'Date'] });
	try {
		// The gap this guards against, distinct from the tolerated brief
		// handoff above: playback stops for LONGER than the tolerance --
		// a genuine stall, not a track-boundary blip -- then recovers before
		// the signal fires. An endpoint-only (or infinitely tolerant) check
		// would miss this entirely.
		const page = fakePage([true, false, false, false, false, false, true]);
		const signal = gate();
		const done = watchContinuousPlaybackUntil(page, signal.promise, {
			pollMs: 1_000,
			tolerateGapsUnderMs: 3_000
		});
		await settle();
		for (let i = 0; i < 6; i += 1) {
			mock.timers.tick(1_000);
			await settle();
		}
		signal.release();
		await assert.rejects(done, /not continuously playing/);
	} finally {
		mock.timers.reset();
	}
});

test('control: a page that never plays at all also rejects (not just the mid-window-drop shape)', async () => {
	mock.timers.enable({ apis: ['setTimeout', 'Date'] });
	try {
		const page = fakePage([false, false]);
		const signal = gate();
		const done = watchContinuousPlaybackUntil(page, signal.promise, {
			pollMs: 1_000,
			tolerateGapsUnderMs: 500
		});
		await settle();
		mock.timers.tick(1_000);
		await settle();
		signal.release();
		await assert.rejects(done, /not continuously playing/);
	} finally {
		mock.timers.reset();
	}
});

test('production default tolerance absorbs a single normal poll-interval gap without invalidating the capture (Sol review, PR #3676)', async () => {
	mock.timers.enable({ apis: ['setTimeout', 'Date'] });
	try {
		// No override: locks in the actual shipped default
		// (TRACKIFY_HANDOFF_TOLERANCE_MS) against the real production pollMs
		// default, so a change to either constant that reintroduces the
		// original false-invalidation bug is caught here.
		const page = fakePage([true, false, true]);
		const signal = gate();
		const done = watchContinuousPlaybackUntil(page, signal.promise, { pollMs: 2_000 });
		await settle();
		mock.timers.tick(2_000);
		await settle();
		mock.timers.tick(2_000);
		await settle();
		signal.release();
		await assert.doesNotReject(done);
	} finally {
		mock.timers.reset();
	}
});

test('a low duty cycle from many short gaps invalidates a Trackify capture even though no single gap is sustained (Sol review, PR #3676)', async () => {
	mock.timers.enable({ apis: ['setTimeout', 'Date'] });
	try {
		// Idle for 3 polls, playing for 1, repeating -- every individual gap
		// recovers well within the single-gap tolerance (2 s, versus a 3 s
		// tolerance here), so the sustained-gap check alone would pass this.
		// But the deck spends 75% of the window not playing, which no real
		// Trackify handoff pattern looks like.
		const page = fakePage([
			false, false, false, true,
			false, false, false, true,
			false, false, false, true
		]);
		const signal = gate();
		const done = watchContinuousPlaybackUntil(page, signal.promise, {
			pollMs: 1_000,
			tolerateGapsUnderMs: 3_000,
			maxIdleDutyCycle: 0.5
		});
		await settle();
		for (let i = 0; i < 11; i += 1) {
			mock.timers.tick(1_000);
			await settle();
		}
		signal.release();
		await assert.rejects(done, /not continuously playing/);
	} finally {
		mock.timers.reset();
	}
});

test('control: a healthy duty cycle with a couple of isolated brief handoffs does not invalidate a Trackify capture (Sol review, PR #3676)', async () => {
	mock.timers.enable({ apis: ['setTimeout', 'Date'] });
	try {
		// Mostly playing, with two isolated single-poll handoffs scattered
		// through a longer window -- representative of Trackify's real
		// operation across several track boundaries, and a control for the
		// duty-cycle check above: it must not fire on ordinary usage.
		const sequence = [
			true, true, true, false, true, true, true, true,
			true, true, false, true, true, true, true, true
		];
		const page = fakePage(sequence);
		const signal = gate();
		const done = watchContinuousPlaybackUntil(page, signal.promise, {
			pollMs: 1_000,
			tolerateGapsUnderMs: 3_000,
			maxIdleDutyCycle: 0.15
		});
		await settle();
		for (let i = 0; i < sequence.length - 1; i += 1) {
			mock.timers.tick(1_000);
			await settle();
		}
		signal.release();
		await assert.doesNotReject(done);
	} finally {
		mock.timers.reset();
	}
});

test('production default duty cycle catches the same thrashing pattern with no overrides (Sol review, PR #3676)', async () => {
	mock.timers.enable({ apis: ['setTimeout', 'Date'] });
	try {
		// No override: locks in the actual shipped default
		// (TRACKIFY_MAX_IDLE_DUTY_CYCLE) against the real production pollMs
		// default.
		const page = fakePage([
			false, false, false, true,
			false, false, false, true,
			false, false, false, true
		]);
		const signal = gate();
		const done = watchContinuousPlaybackUntil(page, signal.promise, { pollMs: 1_000 });
		await settle();
		for (let i = 0; i < 11; i += 1) {
			mock.timers.tick(1_000);
			await settle();
		}
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
	mock.timers.enable({ apis: ['setTimeout', 'Date'] });
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

const PICKED = ['aaa', 'bbb', 'ccc', 'ddd'];
const loadedDecks = () =>
	Object.fromEntries(
		PICKED.map((stableId, index) => [
			index + 1,
			{ stable_id: stableId, duration_ms: 180_000, playing: true }
		])
	);

test('gigBaselineDeckFaults: four decks holding their picked tracks, loaded and playing, have no faults', () => {
	// Positive control for the refusals below: a check that always faulted would pass them.
	assert.deepEqual(gigBaselineDeckFaults(loadedDecks(), PICKED), []);
});

test('gigBaselineDeckFaults: names every deck that is not the picked, loaded, playing track (Sol P1, PR #4540)', () => {
	const cases = [
		[(d) => (d[2] = null), /deck 2 is missing/],
		[(d) => (d[3].stable_id = null), /deck 3 holds null, expected ccc/],
		[(d) => (d[1].stable_id = 'zzz'), /deck 1 holds zzz, expected aaa/],
		[(d) => (d[4].duration_ms = null), /deck 4 duration_ms=null/],
		[(d) => (d[4].duration_ms = 0), /deck 4 duration_ms=0/],
		[(d) => (d[2].playing = false), /deck 2 is not playing/]
	];
	for (const [breakDeck, expected] of cases) {
		const decks = loadedDecks();
		breakDeck(decks);
		const faults = gigBaselineDeckFaults(decks, PICKED);
		assert.equal(faults.length, 1, `${expected}: ${faults.join('; ')}`);
		assert.match(faults[0], expected);
	}
});
