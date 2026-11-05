// requirement: DECKGAP-7691f
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

async function lookApi() {
	return loadTypeScriptModule('src/lib/components/rb/deck/deck-activity-look.ts');
}

function stopped(over = {}) {
	return {
		loaded: true,
		playing: false,
		audible: false,
		transportPending: false,
		commandPending: false,
		positionMs: 10_000,
		durationMs: 180_000,
		loopEngaged: false,
		pausedForMs: 0,
		...over
	};
}

test('playing decks stay bright, including at the end and while looping', async () => {
	const { deckActivityLook } = await lookApi();
	assert.deepEqual(
		deckActivityLook(stopped({ playing: true, positionMs: 180_000, pausedForMs: 60_000 })),
		{ dim: false, looping: false }
	);
	assert.deepEqual(deckActivityLook(stopped({ playing: true, loopEngaged: true })), {
		dim: false,
		looping: true
	});
});

test('a stopped deck dims at the end immediately, and after 30s in the middle', async () => {
	const { deckActivityLook, DECK_PAUSE_DIM_AFTER_MS } = await lookApi();
	assert.equal(deckActivityLook(stopped({ positionMs: 180_000 })).dim, true);
	assert.equal(deckActivityLook(stopped({ positionMs: 179_999 })).dim, false);
	assert.equal(deckActivityLook(stopped({ pausedForMs: DECK_PAUSE_DIM_AFTER_MS - 1 })).dim, false);
	assert.equal(deckActivityLook(stopped({ pausedForMs: DECK_PAUSE_DIM_AFTER_MS })).dim, true);
});

test('an engaged loop is slightly marked even when paused, and is not treated as finished', async () => {
	const { deckActivityLook, DECK_PAUSE_DIM_AFTER_MS } = await lookApi();
	assert.deepEqual(
		deckActivityLook(stopped({ loopEngaged: true, positionMs: 180_000, pausedForMs: 0 })),
		{ dim: false, looping: true }
	);
	assert.deepEqual(
		deckActivityLook(stopped({ loopEngaged: true, pausedForMs: DECK_PAUSE_DIM_AFTER_MS })),
		{ dim: true, looping: true }
	);
});

test('empty decks and a still-settling end stay undimmed', async () => {
	const { deckActivityLook } = await lookApi();
	assert.deepEqual(deckActivityLook(stopped({ loaded: false, loopEngaged: true })), {
		dim: false,
		looping: false
	});
	assert.equal(deckActivityLook(stopped({ positionMs: 180_000, audible: true })).dim, false);
	assert.equal(
		deckActivityLook(stopped({ positionMs: 180_000, transportPending: true })).dim,
		false
	);
	assert.equal(deckActivityLook(stopped({ positionMs: 180_000, commandPending: true })).dim, false);
});
