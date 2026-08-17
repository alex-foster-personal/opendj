import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Regression Mon 17 Aug 2026: "double clicking two songs in succession should
// load two songs into two decks; cmd+doubleclick should replace the song
// already loaded if you've changed your mind." The old inline picker only
// bumped deckLoadSeq AFTER the async load resolved, so two fast double-
// clicks (before the first load settled) both read the same stale seq and
// picked the SAME deck - the second track silently replaced the first.
// - if the picker itself needs a caller-side async round trip to update
//   ordering then two fast double-clicks race and collide -- broken (this
//   is exactly why the picker is now a pure, synchronous function: callers
//   reserve the returned deck immediately, not after the load resolves)
// - if replace=true with no prior double-click falls to null instead of the
//   normal pick then a bare cmd+dblclick before ever double-clicking does
//   nothing -- broken
// - if replace=true ignores lastDoubleClickDeck and advances anyway then
//   "changed my mind" adds a third deck instead of swapping -- broken
// - if shift picks a deck that's currently playing then it silently steals
//   audio out from under the DJ -- broken

async function _mod() {
	return loadTypeScriptModule('src/lib/rb/double-click-deck-pick.ts');
}

function seq(overrides = {}) {
	return { 1: 0, 2: 0, 3: 0, 4: 0, ...overrides };
}

const emptyPair = {
	3: { stable_id: null, playing: false },
	4: { stable_id: null, playing: false }
};

describe('double-click deck pick', () => {
	it('two plain double-clicks in a row pick two different decks (no async round trip needed)', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const first = pickDoubleClickDeck({
			shift: false,
			replace: false,
			deckLoadSeq: seq(),
			lastDoubleClickDeck: null,
			pair: emptyPair
		});
		assert.equal(first.deck, 1);
		// Caller reserves deck 1 (bumps its seq) synchronously, before any
		// await - simulate that here, exactly as BrowserPanel now does.
		const afterReserve = seq({ 1: 1 });
		const second = pickDoubleClickDeck({
			shift: false,
			replace: false,
			deckLoadSeq: afterReserve,
			lastDoubleClickDeck: 1,
			pair: emptyPair
		});
		assert.equal(second.deck, 2);
	});

	it('cmd/ctrl+dblclick replaces the last double-click deck instead of advancing', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const result = pickDoubleClickDeck({
			shift: false,
			replace: true,
			deckLoadSeq: seq({ 1: 1 }),
			lastDoubleClickDeck: 1,
			pair: emptyPair
		});
		assert.equal(result.deck, 1);
	});

	it('cmd/ctrl+dblclick before any plain double-click falls through to the normal pick', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const result = pickDoubleClickDeck({
			shift: false,
			replace: true,
			deckLoadSeq: seq(),
			lastDoubleClickDeck: null,
			pair: emptyPair
		});
		assert.equal(result.deck, 1);
	});

	it('shift prefers an empty CH3/CH4 deck over a stopped one, and never a playing one', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const bothPlaying = pickDoubleClickDeck({
			shift: true,
			replace: false,
			deckLoadSeq: seq(),
			lastDoubleClickDeck: null,
			pair: {
				3: { stable_id: 'a', playing: true },
				4: { stable_id: 'b', playing: true }
			}
		});
		assert.equal(bothPlaying.deck, null);
		assert.match(bothPlaying.error, /both playing/);

		const oneEmpty = pickDoubleClickDeck({
			shift: true,
			replace: false,
			deckLoadSeq: seq(),
			lastDoubleClickDeck: null,
			pair: {
				3: { stable_id: 'a', playing: true },
				4: { stable_id: null, playing: false }
			}
		});
		assert.equal(oneEmpty.deck, 4);
	});
});
