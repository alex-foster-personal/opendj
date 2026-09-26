import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const trackTablePath = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
);

// REQ: LIBUX-18
test('MORE/LESS deck layout preserves the selected row across viewport height changes', () => {
	const src = readFileSync(trackTablePath, 'utf8');
	assert.match(src, /let deckLayoutAnchor:/, 'must anchor the selected row on deck layout toggle');
	assert.match(
		src,
		/if \(layout !== lastDeckLayout\)/,
		'must detect deck_layout preference changes'
	);
	assert.match(
		src,
		/scrollTopForDeckLayoutAnchor\(\{/,
		'must recompute scrollTop when the library viewport height changes'
	);
	assert.match(
		src,
		/rowIndex: anchor\.rowIndex/,
		'must keep the same selected row index visible after layout-driven resize'
	);
});
