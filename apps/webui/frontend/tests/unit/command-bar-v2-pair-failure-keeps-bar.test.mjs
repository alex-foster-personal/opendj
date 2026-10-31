/**
 * CMDK pair failure (Codex P2 BLOCKING, #6691): when one aligned pick fails while an overlapping pick is still
 * loading, the last load to settle must not close the bar and drop that refusal.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');

test('CMDK-pair-failure: the last aligned load keeps the bar open while a refusal is pending', () => {
	assert.match(source, /if \(open && barSession === session && pairLoads\.length === 0 && actionError === null\) closeBar\(\);/);
});
