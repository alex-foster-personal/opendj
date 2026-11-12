import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * KEYB-11383: Backquote cycles the recent (keyboard) deck across loaded decks,
 * and every deck header marks the deck the same resolver picks.
 *
 * Regression lines:
 * - if Backquote can land on an empty deck then broken
 * - if Shift+Backquote does not walk the same ring backwards then broken
 * - if no loaded deck changes the recent deck then broken
 * - if two headers can resolve as the keyboard deck then broken
 */

let rd;

before(async () => {
	rd = await loadTypeScriptModule('src/lib/rb/recent-deck.ts');
});

function walk(start, direction, loaded, steps) {
	const seen = [];
	let cur = start;
	for (let i = 0; i < steps; i += 1) {
		cur = rd.cycleRecentDeck(cur, direction, loaded);
		seen.push(cur);
	}
	return seen;
}

test('forward from 2 over decks 1, 2, 4 goes 4, 1, 2', () => {
	assert.deepEqual(walk(2, 1, [1, 2, 4], 3), [4, 1, 2]);
});

test('backward from 2 over decks 1, 2, 4 goes 1, 4, 2', () => {
	assert.deepEqual(walk(2, -1, [1, 2, 4], 3), [1, 4, 2]);
});

test('no loaded deck leaves the recent deck unchanged', () => {
	assert.equal(rd.cycleRecentDeck(2, 1, []), 2);
	assert.equal(rd.cycleRecentDeck(null, -1, []), null);
});

test('an unloaded current deck steps to the nearest loaded neighbour', () => {
	assert.equal(rd.cycleRecentDeck(3, 1, [1, 2, 4]), 4);
	assert.equal(rd.cycleRecentDeck(3, -1, [1, 2, 4]), 2);
	assert.equal(rd.cycleRecentDeck(null, 1, [2, 4]), 2);
	assert.equal(rd.cycleRecentDeck(null, -1, [2, 4]), 4);
});

test('a single loaded deck stays put', () => {
	assert.equal(rd.cycleRecentDeck(2, 1, [2]), 2);
});

const presence = (loaded, playing = []) =>
	[1, 2, 3, 4].map((id) => ({ id, loaded: loaded.includes(id), playing: playing.includes(id) }));

test('resolveKeyboardDeck: recent loaded wins, then playing, then first loaded, else null', () => {
	assert.equal(rd.resolveKeyboardDeck(3, presence([1, 3], [1])), 3);
	assert.equal(rd.resolveKeyboardDeck(2, presence([1, 3], [3])), 3);
	assert.equal(rd.resolveKeyboardDeck(2, presence([3, 4])), 3);
	assert.equal(rd.resolveKeyboardDeck(null, presence([])), null);
});

test('exactly one header id matches the resolver while any deck is loaded', () => {
	const decks = presence([2, 4], [4]);
	const pick = rd.resolveKeyboardDeck(1, decks);
	assert.equal([1, 2, 3, 4].filter((id) => id === pick).length, 1);
});

test('subscribeRecentDeck notifies on change only and stops after unsubscribe', () => {
	const seen = [];
	const off = rd.subscribeRecentDeck((d) => seen.push(d));
	rd.noteRecentDeck(3);
	rd.noteRecentDeck(3);
	rd.noteRecentDeck(1);
	off();
	rd.noteRecentDeck(2);
	assert.deepEqual(seen, [3, 1]);
	assert.equal(rd.getRecentDeck(), 2);
});
