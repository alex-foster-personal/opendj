// requirement: MIXUX-08
// [if] Shift is held while selecting decks or mixer columns [then] more than one can be selected
// [if] Esc is pressed with multi-select active [then] selection returns to one
// [if] a deck is clicked without Shift [then] only that deck is selected
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let selection;

before(async () => {
	selection = await loadTypeScriptModule('src/lib/rb/mixer-selection.svelte.ts');
});

test('plain click selects a single deck', () => {
	selection.clickSelect(2, false);
	assert.deepEqual(selection.getSelectedDecks(), [2]);
});

test('shift click toggles multi-select', () => {
	selection.resetToSingle(1);
	selection.clickSelect(2, true);
	assert.deepEqual(selection.getSelectedDecks(), [1, 2]);
	selection.clickSelect(2, true);
	assert.deepEqual(selection.getSelectedDecks(), [1]);
});

test('deck load resets to the loading deck only', () => {
	selection.clickSelect(2, true);
	selection.clickSelect(3, true);
	selection.onDeckLoadStart(4);
	assert.deepEqual(selection.getSelectedDecks(), [4]);
});
