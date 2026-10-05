/**
 * @pytest.mark.requirement DECKUX-25
 * Pin a675881be6c8: clicking a deck did not select it for Space and the other
 * transport shortcuts, which kept targeting the deck last loaded or played.
 *
 * [if] a deck panel or mixer channel is clicked [then] that deck is the
 *   transport shortcut target [else stop].
 * [if] a shift-click REMOVES a deck from the selection [then] the target does
 *   not move to the deck just deselected [else stop].
 *
 * Regression lines:
 * - if a click selects the deck visually but Space still drives another deck
 *   then the selection lies -> broken
 * - if deselecting a deck makes it the Space target then the overshoot shipped
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/deck-click-space-target-entry.ts');
});

test('control: the recent deck starts where it was last noted', () => {
	m.noteRecentDeck(1);
	assert.equal(m.getRecentDeck(), 1);
});

test('a plain click makes the clicked deck the transport target', () => {
	m.noteRecentDeck(1);
	m.clickSelect(3, false);
	assert.deepEqual(m.getSelectedDecks(), [3]);
	assert.equal(m.getRecentDeck(), 3);
});

test('a shift-click that adds a deck makes it the transport target', () => {
	m.resetToSingle(1);
	m.noteRecentDeck(1);
	m.clickSelect(2, true);
	assert.deepEqual(m.getSelectedDecks(), [1, 2]);
	assert.equal(m.getRecentDeck(), 2);
});

test('a shift-click that removes a deck does not make it the transport target', () => {
	m.resetToSingle(1);
	m.clickSelect(2, true);
	m.noteRecentDeck(1);
	m.clickSelect(2, true);
	assert.deepEqual(m.getSelectedDecks(), [1]);
	assert.equal(m.getRecentDeck(), 1);
});

test('shift-clicking the only selected deck keeps it selected and targeted', () => {
	m.resetToSingle(4);
	m.noteRecentDeck(1);
	m.clickSelect(4, true);
	assert.deepEqual(m.getSelectedDecks(), [4]);
	assert.equal(m.getRecentDeck(), 4);
});
