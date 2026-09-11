import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let pending;

before(async () => {
	pending = await loadTypeScriptModule('src/lib/rb/deck-slots.ts');
});

test('pending load play is generation-owned, toggleable, consumable, and cleaned after failure', () => {
	const first = pending.beginPendingLoadPlay(1, false);
	assert.equal(first.desiredPlay, false);
	assert.equal(pending.setPendingLoadPlayIntent(1, first.generation, true), true);
	assert.equal(pending.consumePendingLoadPlay(1, first.generation).desiredPlay, true);
	assert.equal(pending.setPendingLoadPlayIntent(1, first.generation + 1, false), false);
	assert.equal(pending.clearPendingLoadPlay(1, first.generation), true);
	assert.equal(pending.consumePendingLoadPlay(1, first.generation), null);

	const doubleClick = pending.beginPendingLoadPlay(2, true);
	assert.equal(doubleClick.desiredPlay, true, 'Load and play begins as an explicit true intent');
	assert.equal(pending.setPendingLoadPlayIntent(2, doubleClick.generation, false), true);
	assert.equal(pending.consumePendingLoadPlay(2, doubleClick.generation).desiredPlay, false);
	assert.equal(pending.clearPendingLoadPlay(2, doubleClick.generation), true);
});

test('Space targets the latest reserved deck through the shared intent state', () => {
	const earlier = pending.beginPendingLoadPlay(1, false);
	const later = pending.beginPendingLoadPlay(3, true);
	assert.equal(pending.mostRecentPendingLoadPlay().generation, later.generation);
	assert.equal(pending.setPendingLoadPlayIntent(3, later.generation, false), true);
	assert.equal(pending.mostRecentPendingLoadPlay().desiredPlay, false);
	assert.equal(pending.clearPendingLoadPlay(1, earlier.generation), true);
	assert.equal(pending.clearPendingLoadPlay(3, later.generation), true);
});
