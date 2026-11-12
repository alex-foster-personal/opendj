import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const PANEL = fileURLToPath(
	new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
);

test('BrowserPanel wires smartlist preview cap and match_total', () => {
	const src = readFileSync(PANEL, 'utf8');
	assert.match(
		src,
		/getSmartlistTracks\([^,]+,\s*SMARTLIST_PREVIEW_CAP\)/,
		'library-browser smartlist fetch must pass the named preview cap'
	);
	assert.match(
		src,
		/result\.match_total/,
		'completeLoad must forward match_total from the fetch result'
	);
	assert.match(src, /p\.match_total\s*=/, 'quiet refresh must assign match_total');
});
