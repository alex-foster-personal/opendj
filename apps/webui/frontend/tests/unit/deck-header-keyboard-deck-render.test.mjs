/**
 * KEYB-11383: the deck header of the recent (keyboard) deck renders
 * data-keyboard-deck="true" and the marker; every other header renders "false".
 */
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as DeckHeader } from '$lib/components/rb/deck/DeckHeader.svelte';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { noteRecentDeck } from '$lib/rb/recent-deck';",
	"export { render } from 'svelte/server';"
].join('\n');

/** @type {Awaited<ReturnType<typeof loadSvelteSsrModule>>} */
let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

beforeEach(() => {
	for (const id of [1, 2, 3, 4]) {
		const deck = mod.deckStates[id];
		deck.stable_id = null;
		deck.playing = false;
	}
});

function load(deckId) {
	const deck = mod.deckStates[deckId];
	deck.stable_id = `fixture-${deckId}`;
	deck.title = `Fixture ${deckId}`;
	return deck;
}

function renderHeader(deckId) {
	return mod.render(mod.DeckHeader, {
		props: {
			deck: mod.deckStates[deckId],
			deckId,
			pending: false,
			onBeatSync: async () => {},
			onMaster: async () => {},
			onMasterTempo: async () => {},
			onKeySync: async () => {},
			onKeyNudge: async () => {},
			onResetTempo: async () => {},
			onUnload: async () => {},
			keySyncAvailable: true,
			mirrored: false
		}
	}).body;
}

function markedDecks() {
	return [1, 2, 3, 4].filter((id) => /data-keyboard-deck="true"/.test(renderHeader(id)));
}

test('exactly one header carries data-keyboard-deck="true" and it follows the recent deck', () => {
	load(1);
	load(2);
	load(4);
	mod.noteRecentDeck(2);
	assert.deepEqual(markedDecks(), [2]);
	const html = renderHeader(2);
	assert.match(html, /data-testid="keyboard-deck-marker-2"/);
	assert.match(html, /keyboard deck/);
	for (const id of [1, 4]) {
		const other = renderHeader(id);
		assert.match(other, /data-keyboard-deck="false"/);
		assert.doesNotMatch(other, /keyboard-deck-marker/);
		assert.match(other, new RegExp(`title="Deck ${id}"`));
	}
	mod.noteRecentDeck(4);
	assert.deepEqual(markedDecks(), [4]);
});

test('a recent deck that is not loaded falls back to the first loaded deck', () => {
	load(1);
	load(2);
	mod.noteRecentDeck(3);
	assert.deepEqual(markedDecks(), [1]);
});

test('with no deck loaded no header is marked', () => {
	mod.noteRecentDeck(2);
	assert.deepEqual(markedDecks(), []);
});
