import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('TrackTable draws full-width row separators on tbody tr', async () => {
	const src = await readFile('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');
	assert.match(src, /tbody tr::after/);
	assert.match(src, /tbody td[\s\S]*border-bottom: none/);
});
