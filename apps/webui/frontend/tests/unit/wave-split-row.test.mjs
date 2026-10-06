import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Split main waveform partner selection (wave_split_master).
// Regression lines:
// - if a follower's top half is not the master deck then broken
// - if the master's top half shows itself or an empty deck then broken
// - if a lone loaded deck gets a partner (instead of staying one-sided) then broken
// - if 'auto' turns split on outside mono-dev, or 'on'/'off' are ignored, then broken

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
});

test('master pairs with the next loaded deck, wrapping, never an empty deck', () => {
	assert.deepEqual(split.splitPartnerDeck(2, decks([1, 2, 4], 2)), { id: 4, label: 'DECK 4' });
	assert.deepEqual(split.splitPartnerDeck(4, decks([1, 4], 4)), { id: 1, label: 'DECK 1' });
});

test('no master elected: next loaded deck, labeled as a deck, not MASTER', () => {
	assert.deepEqual(split.splitPartnerDeck(1, decks([1, 3], null)), { id: 3, label: 'DECK 3' });
});

test('a lone loaded deck has no partner', () => {
	assert.equal(split.splitPartnerDeck(1, decks([1], 1)), null);
	assert.equal(split.splitPartnerDeck(1, decks([1], null)), null);
});

test('wave_split_master: auto follows the skin, on/off override', () => {
	assert.equal(skin.waveSplitActive('auto', 'mono-dev'), true);
	assert.equal(skin.waveSplitActive('auto', 'default'), false);
	assert.equal(skin.waveSplitActive('on', 'default'), true);
	assert.equal(skin.waveSplitActive('off', 'mono-dev'), false);
	assert.throws(() => skin.parseWaveSplitMaster('sometimes'));
});
