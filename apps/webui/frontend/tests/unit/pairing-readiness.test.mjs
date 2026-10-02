/**
 * DECKUX-12, Mac check on PR #4014 (Fri 2 Oct 2026): Create pairing failed
 * AFTER the click with "CH1 has no beatgrid timestamp" for a deck parked at
 * 0:00 before its first beat. pairingBeatAt reads that lead-in as the beat
 * before the first one; pairingUnavailableReason disables the button, with the
 * reason as its tooltip, only when capture genuinely cannot work.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
before(async () => {
	mod = {
		...(await loadTypeScriptModule('src/lib/rb/pairing-readiness.ts')),
		...(await loadTypeScriptModule('src/lib/rb/pairing-button-state.ts'))
	};
});

const GRID = [
	{ n: 1, bpm: 120, t: 0.05 },
	{ n: 2, bpm: 120, t: 0.55 },
	{ n: 3, bpm: 120, t: 1.05 }
];

test('a playhead before the first beat is on the beat before it, not gridless', () => {
	assert.equal(mod.pairingBeatAt(GRID, 0), 4);
	// Two beats back from beat 1 at 120 BPM (500 ms per beat).
	assert.equal(mod.pairingBeatAt([{ n: 1, bpm: 120, t: 1.0 }], 100), 3);
});

test('control: inside the grid it is still the last beat at or before the playhead', () => {
	assert.equal(mod.pairingBeatAt(GRID, 50), 1);
	assert.equal(mod.pairingBeatAt(GRID, 549), 1);
	assert.equal(mod.pairingBeatAt(GRID, 550), 2);
	assert.equal(mod.pairingBeatAt(GRID, 99_000), 3);
});

test('an empty grid has no beat to record', () => {
	assert.equal(mod.pairingBeatAt([], 0), null);
});

const deck = (deckId, loaded, beatCount) => ({ deckId, loaded, beatCount });

test('fewer than two loaded decks disables the button and says so', () => {
	assert.match(
		mod.pairingUnavailableReason([deck(1, true, 3), deck(2, false, 0)], false),
		/Load a track on two decks/
	);
});

test('with BeatSyncMax on, a loaded deck with no beatgrid disables the button and names the deck', () => {
	const reason = mod.pairingUnavailableReason([deck(1, true, 0), deck(2, true, 8)], true);
	assert.match(reason, /CH1 has no beatgrid/);
	assert.match(reason, /BeatSyncMax/);
	assert.match(
		mod.pairingUnavailableReason([deck(1, true, 0), deck(2, true, 0)], true),
		/^CH1 and CH2 have no beatgrid/
	);
});

test('control: the same gridless deck is fine with BeatSyncMax off (time is recorded)', () => {
	assert.equal(mod.pairingUnavailableReason([deck(1, true, 0), deck(2, true, 8)], false), null);
});

test('control: two loaded gridded decks enable the button with BeatSyncMax on', () => {
	assert.equal(
		mod.pairingUnavailableReason([deck(1, true, 4), deck(2, true, 8), deck(3, false, 0)], true),
		null
	);
});
