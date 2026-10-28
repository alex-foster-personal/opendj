/**
 * Codex (PR #5822, BLOCKING): a plan's prepare cue must land at its authored
 * position even under coarse quantize, and the mirror must say whether an
 * outgoing deck's loop is engaged, so validation can refuse a trapped window.
 * Asserted in source: the seek command's parse and dispatch are the contract.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const source = await readFile(new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url), 'utf8');

test('a seek may carry exact, which the executor forwards as skipGridQuantize', () => {
	assert.match(source, /_exactKeys\(record, \['type', 'deck', 'position_ms', 'exact'\]\);/);
	assert.match(source, /await engine\.quantizedSeek\(command\.deck, command\.position_ms, command\.exact === true\);/);
});

test('the deck mirror publishes whether its loop is engaged', () => {
	assert.match(source, /loop_engaged: boolean;/);
	assert.match(source, /loop_engaged: deck\.loop !== null && deck\.loop\.engaged,/);
});
