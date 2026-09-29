// requirement: LYRICS-04
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

test('pin 64b114a0dd72 lyric results mount below TrackTable with divider label', () => {
	const panel = source('src/lib/components/rb/BrowserPanel.svelte');
	const results = source('src/lib/components/rb/browser/LyricSearchResults.svelte');
	assert.match(panel, /LyricSearchResults/);
	assert.match(panel, /primarySettled=\{!pane\.searching\}/);
	assert.match(results, /Lyric matches/);
	assert.match(results, /lsr-snippet/);
	assert.match(results, /lsr-divider/);
});
