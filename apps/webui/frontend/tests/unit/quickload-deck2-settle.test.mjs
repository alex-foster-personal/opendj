/**
 * Issue #13217 ((private path), section 1): the
 * deck-two quickload wait must fail fast on a settled WRONG stable id, pass
 * on the requested id, and carry any deck-error seen while waiting.
 *
 * Pins tests/e2e/support/deck-settle.ts, the pure step behind
 * quickload-dblclick-guard.spec.ts's `_waitForDeckLoaded`, without a browser.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SETTLE = 'tests/e2e/support/deck-settle.ts';
const WANT = 'aaaaaaaa-requested-row';
const OTHER = 'bbbbbbbb-reordered-row';

// Build a four-deck board with deck 2 set to the given snapshot fields.
function board(deck2) {
	const empty = { stable_id: null, playing: false, transport_pending: false };
	return {
		decks: { 1: { ...empty }, 2: { ...empty, ...deck2 }, 3: { ...empty }, 4: { ...empty } }
	};
}

// A settled wrong id on deck 2 stops the poll at once and names both ids plus the roster.
test('if deck two settles on a different stable id then the wait fails fast naming both ids', async () => {
	const { deckSettleTick, deckSettleFailure } = await loadTypeScriptModule(SETTLE);
	const tick = deckSettleTick(board({ stable_id: OTHER }), 2, WANT, []);
	assert.equal(tick.value, WANT, 'the poll must stop this tick, not sit out the 45 s timeout');
	assert.ok(tick.settledWrong instanceof Error, 'a wrong-id load must yield an error to throw');
	const msg = tick.settledWrong.message;
	assert.match(msg, /deck 2 settled on bbbbbbbb-reordered-row, expected aaaaaaaa-requested-row/);
	assert.match(msg, /board: deck1\[sid=null .*deck2\[sid=bbbbbbbb playing=false pending=false\]/);
	const thrown = deckSettleFailure(new Error('poll timeout'), tick.settledWrong, []);
	assert.equal(thrown, tick.settledWrong, 'the wrong-id error wins over the poll error');
});

// While transport is pending a different id is not yet a verdict: keep polling.
test('if deck two holds a different id with transport pending then the wait keeps polling', async () => {
	const { deckSettleTick } = await loadTypeScriptModule(SETTLE);
	const tick = deckSettleTick(board({ stable_id: OTHER, transport_pending: true }), 2, WANT, []);
	assert.equal(tick.settledWrong, undefined);
	assert.equal(tick.value, `${OTHER} (pending)`);
	assert.notEqual(tick.value, WANT);
});

// The requested id with no transport pending is the pass: the poll value equals the expected id.
test('if deck two settles on the requested stable id then the wait passes', async () => {
	const { deckSettleTick } = await loadTypeScriptModule(SETTLE);
	const tick = deckSettleTick(board({ stable_id: WANT }), 2, WANT, ['deck-error stale']);
	assert.equal(tick.value, WANT);
	assert.equal(tick.settledWrong, undefined, 'the right track must not fail');
	const empty = deckSettleTick(board({}), 2, WANT, []);
	assert.equal(empty.value, null, 'an empty deck is not yet loaded');
	assert.equal(empty.settledWrong, undefined);
});

// Deck-error console lines collected during the wait ride both the wrong-id error and the timeout.
test('if a deck error is emitted while waiting then the failure carries it', async () => {
	const { deckSettleTick, deckSettleFailure } = await loadTypeScriptModule(SETTLE);
	const errors = ['deck-error a', 'deck-error b', 'deck-error c', 'deck-error Failed to fetch'];
	const tick = deckSettleTick(board({ stable_id: OTHER }), 2, WANT, errors);
	assert.match(
		tick.settledWrong.message,
		/; deck-error: deck-error b \| deck-error c \| deck-error Failed to fetch$/
	);
	const timeout = new Error('deck 2 never settled holding aaaaaaaa-requested-row');
	const wrapped = deckSettleFailure(timeout, undefined, errors);
	assert.ok(wrapped instanceof Error);
	assert.notEqual(wrapped, timeout);
	assert.match(
		wrapped.message,
		/^deck 2 never settled holding aaaaaaaa-requested-row; deck-error: /
	);
	assert.match(wrapped.message, /Failed to fetch/);
	assert.equal(deckSettleFailure(timeout, undefined, []), timeout, 'no deck-error: original error');
});
