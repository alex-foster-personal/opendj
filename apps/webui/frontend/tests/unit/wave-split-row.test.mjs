import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Split main waveform partner selection (wave_split_master, opt-in).
// Regression lines:
// - if a follower's top half is not the master deck then broken
// - if the master's own row gets ANY partner deck on its top half (instead of a plain mirrored row) then broken
// - if a lone loaded deck gets a partner (instead of a plain mirrored row) then broken
// - if 'auto' turns split on for ANY skin, or 'on'/'off' are ignored, then broken

let split;
let skin;

before(async () => {
	split = await loadTypeScriptModule('src/lib/components/rb/wave/split-row.ts');
	skin = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
});

const decks = (loaded, master) =>
	[1, 2, 3, 4].map((id) => ({ id, loaded: loaded.includes(id), isMaster: id === master }));

test('follower pairs with the master', () => {
	assert.deepEqual(split.splitPartnerDeck(3, decks([1, 2, 3], 2)), { id: 2, label: 'MASTER 2' });
	assert.deepEqual(split.splitPartnerDeck(1, decks([1, 4], 4)), { id: 4, label: 'MASTER 4' });
});

test('the master row has no partner: it paints this deck top and bottom (mirrored)', () => {
	assert.equal(split.splitPartnerDeck(2, decks([1, 2, 4], 2)), null);
	assert.equal(split.splitPartnerDeck(4, decks([1, 4], 4)), null);
	assert.equal(split.splitPartnerDeck(2, decks([1, 2, 3, 4], 2)), null);
});

test('no master elected: next loaded deck, wrapping, labeled as a deck, not MASTER', () => {
	assert.deepEqual(split.splitPartnerDeck(1, decks([1, 3], null)), { id: 3, label: 'DECK 3' });
	assert.deepEqual(split.splitPartnerDeck(4, decks([2, 4], null)), { id: 2, label: 'DECK 2' });
});

test('a lone loaded deck has no partner', () => {
	assert.equal(split.splitPartnerDeck(1, decks([1], 1)), null);
	assert.equal(split.splitPartnerDeck(1, decks([1], null)), null);
});

test('wave_split_master: auto is OFF on every skin, on/off are explicit', () => {
	// waveSplitActive takes no skin: auto cannot follow one back on.
	assert.equal(skin.waveSplitActive.length, 1);
	assert.equal(skin.waveSplitActive('auto'), false, 'auto is off');
	assert.equal(skin.WAVE_SPLIT_MASTER_DEFAULT, 'auto');
	assert.equal(skin.waveSplitActive('on'), true, 'on stays an explicit opt-in');
	assert.equal(skin.waveSplitActive('off'), false);
	assert.throws(() => skin.parseWaveSplitMaster('sometimes'));
});
