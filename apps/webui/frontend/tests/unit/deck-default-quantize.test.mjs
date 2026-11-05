// requirement: SETSTUB-02
// [if] "Quantize on new decks" is off [then] a fresh or ejected deck starts with Q off, else broken
// [if] the pref changes [then] every EMPTY deck follows and a LOADED deck keeps its own Q, else broken
// [if] no pref was ever written [then] decks start with Q on (the historical default), else broken
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const ENTRY = 'tests/unit/fixtures/deck-default-quantize-entry.ts';
const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

function installFakeWindow(initial = {}) {
	const store = new Map(Object.entries(initial));
	globalThis.window = {
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	return store;
}

async function load(initial) {
	const store = installFakeWindow(initial);
	globalThis.fetch = async () => {
		throw new Error('no daemon under node tests');
	};
	const mod = await loadTypeScriptModule(ENTRY);
	return { mod, store };
}

test('with no stored pref every deck starts with Quantize on', async () => {
	const { mod } = await load();
	assert.equal(mod.uiPrefs.deck_default_quantize, true);
	for (const deck of [1, 2, 3, 4])
		assert.equal(mod.deckStates[deck].quantize_enabled, true, `deck ${deck}`);
	assert.equal(mod._emptyDeckState(1).quantize_enabled, true);
});

test('a stored off pref seeds the four page-load decks and every ejected deck with Quantize off', async () => {
	const { mod } = await load({
		[STORAGE_KEY]: JSON.stringify({ hide_broken_links: false, deck_default_quantize: false })
	});
	assert.equal(mod.uiPrefs.deck_default_quantize, false, 'control: the stored pref was read');
	for (const deck of [1, 2, 3, 4])
		assert.equal(mod.deckStates[deck].quantize_enabled, false, `deck ${deck}`);
	assert.equal(
		mod._emptyDeckState(2).quantize_enabled,
		false,
		'the eject/dispose reset follows the pref'
	);
});

test('changing the pref updates empty decks only, persists locally, and reads back through apply', async () => {
	const { mod, store } = await load();
	// Deck 1 holds a track whose DJ turned Q on; it must keep it.
	mod.deckStates[1].stable_id = 'track-1';
	mod.deckStates[1].quantize_enabled = true;

	mod.applySettingChange('deck_default_quantize', false);
	assert.equal(mod.readSettingValue('deck_default_quantize'), false);
	assert.equal(mod.deckStates[1].quantize_enabled, true, 'a loaded deck keeps its own Q');
	for (const deck of [2, 3, 4])
		assert.equal(mod.deckStates[deck].quantize_enabled, false, `empty deck ${deck}`);
	assert.equal(mod._emptyDeckState(1).quantize_enabled, false);
	assert.equal(
		JSON.parse(store.get(STORAGE_KEY)).deck_default_quantize,
		false,
		'persisted to the local blob'
	);

	// Opposite direction: turning it back on re-enables the empty decks.
	mod.setDeckDefaultQuantize(true);
	for (const deck of [2, 3, 4])
		assert.equal(mod.deckStates[deck].quantize_enabled, true, `empty deck ${deck}`);
	assert.equal(mod._emptyDeckState(1).quantize_enabled, true);
});

test('a non-boolean value is refused rather than seeding a deck with it', async () => {
	const { mod } = await load();
	assert.throws(() => mod.setDeckDefaultQuantize('no'), /deck_default_quantize must be a boolean/);
	assert.equal(mod.uiPrefs.deck_default_quantize, true);
	await assert.rejects(
		() =>
			load({
				[STORAGE_KEY]: JSON.stringify({ hide_broken_links: false, deck_default_quantize: 'off' })
			}),
		/deck_default_quantize is not a boolean/
	);
});
