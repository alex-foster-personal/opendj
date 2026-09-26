/**
 * MIXUX-09: deck column and mixer strip share hover chrome tokens.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
test('deck and mixer deck-focus use the same hover CSS variables', () => {
	const page = readFileSync(
		new URL('../../src/routes/performance/+page.svelte', import.meta.url),
		'utf8'
	);
	const deck = readFileSync(
		new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url),
		'utf8'
	);
	const strip = readFileSync(
		new URL('../../src/lib/components/rb/mixer/ChannelStrip.svelte', import.meta.url),
		'utf8'
	);
	assert.match(page, /--rb-deck-hover-inset:/);
	assert.match(page, /--rb-deck-hover-bg:/);
	assert.match(deck, /var\(--rb-deck-hover-inset\)/);
	assert.match(deck, /var\(--rb-deck-hover-bg\)/);
	assert.match(strip, /var\(--rb-deck-hover-inset\)/);
	assert.match(strip, /var\(--rb-deck-hover-bg\)/);
});
