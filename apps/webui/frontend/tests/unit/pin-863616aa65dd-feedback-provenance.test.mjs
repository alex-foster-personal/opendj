// requirement: FB-09
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

test('pin 863616aa65dd environment visible on pin card', () => {
	const card = source('src/lib/components/rb/FeedbackPinCard.svelte');
	assert.match(card, /data-testid="fb-pin-provenance"/);
	assert.match(card, /pin\.environment\.ui/);
	assert.match(card, /pin\.build\?\.git_sha/);
	assert.match(card, /Created \(UTC\)/);
});
