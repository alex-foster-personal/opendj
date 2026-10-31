/**
 * CMDK eject generation (Codex BLOCKING, #6691): an eject that waits for a queued load to drain must
 * re-check the load generation the pick read, and refuse rather than unload the track that landed.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');

test('CMDK-eject-generation: the eject re-checks the load generation after its drain wait', () => {
	assert.match(source, /const picked = decks\[command\.deck\]\.load_generation;/);
	assert.match(source, /if \(decks\[command\.deck\]\.load_generation !== picked\) return fail\(/);
});
