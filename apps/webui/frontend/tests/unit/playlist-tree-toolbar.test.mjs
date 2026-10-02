import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(rel) {
	return readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');
}

test('library toolbar hosts tree/column toggle and undo/redo icons', () => {
	const tabs = source('src/lib/components/rb/browser/LibrarySourceTabs.svelte');
	assert.match(tabs, /data-testid="library-tree-toolbar"/);
	assert.match(tabs, /data-testid="playlist-tree-view-toggle"/);
	assert.match(tabs, /data-testid="playlist-undo"/);
	assert.match(tabs, /data-testid="playlist-redo"/);
	assert.match(tabs, /history-list/);
});

test('playlist tree view persists via setPlaylistTreeView', () => {
	const prefs = source('src/lib/rb/prefs.svelte.ts');
	const hydrate = source('src/lib/rb/prefs-hydrate.ts');
	assert.match(prefs, /setPlaylistTreeView/);
	assert.match(hydrate, /playlist_tree_view/);
});

test('Deck applies the mirror class: decks 2+4 follow the right pref, decks 1+3 the left (pin e207535123b9)', () => {
	const deck = source('src/lib/components/rb/Deck.svelte');
	assert.match(deck, /mirror-main-row/);
	assert.match(
		deck,
		/class:mirror-main-row=\{\(deckId === 2 \|\| deckId === 4\) \? uiPrefs\.deck_right_mirror : uiPrefs\.deck_left_mirror\}/
	);
});
