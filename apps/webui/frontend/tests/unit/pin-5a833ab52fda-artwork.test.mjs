// requirement: LIBUX-26
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

test('pin 5a833ab52fda TrackTable artwork uses two-thirds anchor and listing gate', () => {
	const table = source('src/lib/components/rb/browser/TrackTable.svelte');
	assert.match(table, /object-position:\s*center 66\.67%/);
	assert.match(table, /_showArtworkImg/);
	assert.match(table, /artworkAvailable === true/);
	// LIBM-181: the gate reads the server verdict through artworkFetchAllowed
	// (shouldFetchArtwork underneath); an image error no longer writes it.
	assert.match(table, /artworkFetchAllowed\(stableId, coverRetry\)/);
	assert.doesNotMatch(table, /artwork:\s*false/);
	assert.match(table, /art-loaded/);
});
