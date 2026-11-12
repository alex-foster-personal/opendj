/**
 * @pytest.mark.requirement MIDI-12638
 * Pin a675881be6c8: FLX10-style MIDI deck pair select drives mixer selection.
 *
 * [if] a mapped MIDI deck-select fires for pair 1-3 or 2-4 [then] mixer
 *   selection updates for that pair [else stop].
 * [if] the other pair already has a deck selected [then] two decks can stay
 *   selected at once [else stop].
 */
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/midi-deck-pair-select-entry.ts');
});

beforeEach(() => {
	m.resetMixerSelectionForTests();
});

test('[if] user clicks a deck panel [then] that deck is selected for transport', () => {
	m.noteRecentDeck(2);
	m.clickSelect(3, false);
	assert.deepEqual(m.getSelectedDecks(), [3]);
	assert.equal(m.getRecentDeck(), 3);
});

test('[if] deck 1 is selected [then] Space transport targets deck 1', () => {
	m.clickSelect(1, false);
	assert.equal(m.getRecentDeck(), 1);
});

test('[if] MIDI pair 1-3 toggles [then] selection moves between 1 and 3', () => {
	m.resetToSingle(1);
	m.midiSelectDeckPair('1-3');
	assert.deepEqual(m.getSelectedDecks(), [3]);
	assert.equal(m.getRecentDeck(), 3);
	m.midiSelectDeckPair('1-3');
	assert.deepEqual(m.getSelectedDecks(), [1]);
});

test('[if] MIDI pair 2-4 fires while 1-3 is on deck 1 [then] two decks stay selected', () => {
	m.resetToSingle(1);
	m.midiSelectDeckPair('2-4');
	assert.deepEqual(m.getSelectedDecks(), [1, 2]);
	assert.equal(m.getRecentDeck(), 2);
});
