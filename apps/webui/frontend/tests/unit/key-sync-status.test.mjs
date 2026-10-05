/**
 * DECKUX-34: deriveKeySyncStatus is the single rule the KEY SYNC light, the
 * IPC snapshot and the engine follow loop read.
 *
 * - if an unarmed deck reads anything but 'off' then the light can come on by itself
 * - if the master reads 'following' then a deck can claim to follow itself
 * - if an arm with no loaded master reads 'following' then restore lights a wrong key
 * - if an arm without a Camelot key on either side reads 'following' then the follow cannot be derived
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/player/key/key-sync-status.ts');
});

function decks(overrides) {
	const base = (id) => ({ stable_id: `sid-${id}`, key: '8A', key_sync_enabled: false, is_master: false });
	return Object.fromEntries([1, 2, 3, 4].map((id) => [id, { ...base(id), ...(overrides[id] ?? {}) }]));
}

test('an unarmed deck is off whatever the master state', () => {
	assert.equal(mod.deriveKeySyncStatus(2, decks({ 1: { is_master: true } })), 'off');
	assert.equal(mod.deriveKeySyncStatus(2, decks({})), 'off');
});

test('an armed follower of a loaded keyed master is following', () => {
	assert.equal(
		mod.deriveKeySyncStatus(2, decks({ 1: { is_master: true }, 2: { key_sync_enabled: true, key: '3A' } })),
		'following'
	);
});

test('an armed deck waits when no loaded master exists', () => {
	assert.equal(mod.deriveKeySyncStatus(2, decks({ 2: { key_sync_enabled: true } })), 'waiting-no-master');
	assert.equal(
		mod.deriveKeySyncStatus(2, decks({ 1: { is_master: true, stable_id: null }, 2: { key_sync_enabled: true } })),
		'waiting-no-master'
	);
});

test('an armed deck that is itself master waits', () => {
	assert.equal(
		mod.deriveKeySyncStatus(2, decks({ 2: { key_sync_enabled: true, is_master: true } })),
		'waiting-is-master'
	);
});

test('an armed deck waits when either side has no Camelot key', () => {
	assert.equal(
		mod.deriveKeySyncStatus(2, decks({ 1: { is_master: true, key: null }, 2: { key_sync_enabled: true } })),
		'waiting-no-key'
	);
	assert.equal(
		mod.deriveKeySyncStatus(2, decks({ 1: { is_master: true }, 2: { key_sync_enabled: true, key: 'C major?' } })),
		'waiting-no-key'
	);
});

test('every waiting status has operator copy that says it is not following', () => {
	for (const status of ['waiting-no-master', 'waiting-is-master', 'waiting-no-key']) {
		assert.match(mod.keySyncStatusTitle(status), /ARMED - not following/);
	}
	assert.throws(() => mod.deriveKeySyncStatus(5, decks({})), RangeError);
});
