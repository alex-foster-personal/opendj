/**
 * CMDK coordinated hold (Codex BLOCKING, #6692): the hold's callback refuses when a master or Beat Sync
 * command was counted while the hold waited, instead of seeking and starting the partner under it.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');

test('CMDK-hold-overtaken: the coordinated hold refuses a master or Beat Sync command that moved after it queued', () => {
	assert.match(source, /const masterAt = deckCommands\(master\.deck\);/);
	assert.match(source, /const syncAt = deckCommands\(deck, 'beat_sync'\);/);
	assert.match(source, /if \(deckCommands\(master\.deck\) !== masterAt \|\| deckCommands\(deck, 'beat_sync'\) !== syncAt\) \{/);
});
