// requirement: STEM-37
/**
 * Session / rescue stem restore (src/lib/rb/stem-restore.ts) against the
 * stem-hydration lifetime (src/lib/rb/stem-hydrate-wait.ts).
 *
 * Regression lines:
 * - [if] restore gives up before the deck's own hydration wait can finish
 *   [then] saved mute / solo / gain are dropped on a spoke whose bundle is
 *   still downloading from R2
 * - [if] restore waits without any bound [then] a deck stuck in `loading`
 *   holds the restore open forever instead of failing loud
 * - [if] the deck loads another track mid-wait [then] the old track's stem
 *   controls must not land on the new one
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let restore;
let hydrate;

before(async () => {
	restore = await loadTypeScriptModule('src/lib/rb/stem-restore.ts');
	hydrate = await loadTypeScriptModule('src/lib/rb/stem-hydrate-wait.ts');
});

const READY = { status: 'ready', error: null, available_controls: ['vocal', 'instrumental', 'drums'] };
const LOADING = { status: 'loading', error: null, available_controls: [] };
const SAVED = { vocal: { muted: true, solo: false, gain: 0.25 } };

/** A deck whose stems stay `loading` until `readyAtMs` on an injected clock.
 * The clock only moves when restore sleeps, so the test runs in real
 * milliseconds while covering minutes of deck time. Every so often a sleep
 * yields a real macrotask, so a wait with no bound is failed by
 * `withinRealMs` instead of starving the event loop and hanging the suite. */
function hydratingDeck(readyAtMs) {
	const state = { nowMs: 0, sleeps: 0, stableId: 'a'.repeat(40), generation: 7, dispatched: [] };
	return {
		state,
		query: () => ({
			decks: {
				1: {
					stable_id: state.stableId,
					load_generation: state.generation,
					stems: state.nowMs >= readyAtMs ? READY : LOADING
				}
			}
		}),
		dispatch: async (command) => {
			state.dispatched.push(command);
			return {};
		},
		clock: {
			now: () => state.nowMs,
			sleep: async (ms) => {
				state.nowMs += ms;
				state.sleeps += 1;
				if (state.sleeps % 500 === 0) await new Promise((resolve) => setImmediate(resolve));
			}
		}
	};
}

/** Fail instead of hanging when the code under test ignores the injected clock. */
function withinRealMs(promise, ms, what) {
	let timer;
	const guard = new Promise((_, reject) => {
		timer = setTimeout(() => reject(new Error(`${what}: no answer within ${ms} real ms`)), ms);
	});
	return Promise.race([promise, guard]).finally(() => clearTimeout(timer));
}

test('the restore deadline outlives the deck stem-hydration wait', () => {
	assert.equal(typeof restore.STEM_RESTORE_MAX_WAIT_MS, 'number');
	assert.ok(
		restore.STEM_RESTORE_MAX_WAIT_MS > hydrate.STEM_HYDRATE_MAX_WAIT_MS,
		`restore gives up after ${restore.STEM_RESTORE_MAX_WAIT_MS} ms, before the deck's own ` +
			`${hydrate.STEM_HYDRATE_MAX_WAIT_MS} ms hydration wait can settle`
	);
});

test('saved stem controls are applied when the bundle hydrates minutes after the restore began', async () => {
	const deck = hydratingDeck(5 * 60 * 1000);
	await withinRealMs(
		restore.restoreStemControls(deck.dispatch, deck.query, 1, SAVED, deck.clock),
		5000,
		'restore through a 5 minute hydration'
	);
	assert.ok(deck.state.nowMs >= 5 * 60 * 1000, `waited only ${deck.state.nowMs} ms of deck time`);
	assert.deepEqual(deck.state.dispatched, [
		{ type: 'stem_mute', deck: 1, stem: 'vocal', muted: true },
		{ type: 'stem_solo', deck: 1, stem: 'vocal', solo: false },
		{ type: 'stem_gain', deck: 1, stem: 'vocal', value: 0.25 }
	]);
});

test('a deck that never leaves loading still fails loud, at the restore deadline', async () => {
	const deck = hydratingDeck(Number.POSITIVE_INFINITY);
	await assert.rejects(
		withinRealMs(
			restore.restoreStemControls(deck.dispatch, deck.query, 1, SAVED, deck.clock),
			5000,
			'restore of a deck stuck in loading'
		),
		/deck 1 stem restore timed out/
	);
	assert.ok(deck.state.nowMs >= restore.STEM_RESTORE_MAX_WAIT_MS, `gave up at ${deck.state.nowMs} ms`);
	assert.deepEqual(deck.state.dispatched, []);
});

test('a deck that loads another track mid-wait refuses the old controls', async () => {
	const deck = hydratingDeck(60 * 1000);
	const clock = {
		now: deck.clock.now,
		sleep: async (ms) => {
			await deck.clock.sleep(ms);
			if (deck.state.nowMs >= 1000) deck.state.generation = 8;
		}
	};
	await assert.rejects(
		withinRealMs(restore.restoreStemControls(deck.dispatch, deck.query, 1, SAVED, clock), 5000, 'stale restore'),
		/deck 1 changed during stem restore/
	);
	assert.deepEqual(deck.state.dispatched, []);
});
