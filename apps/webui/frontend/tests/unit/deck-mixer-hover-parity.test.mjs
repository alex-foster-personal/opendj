/**
 * MIXUX-09 / DECKUX-5917: deck column and mixer strip share the link tick tokens.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
test('deck and mixer deck-focus use the shared link tick tokens', () => {
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
	assert.match(page, /--rb-deck-link-tick:\s*inset 3px 0 0 var\(--rb-accent/);
	assert.match(page, /--rb-deck-link-tick-top:\s*inset 0 3px 0 var\(--rb-accent/);
	assert.match(deck, /var\(--rb-deck-link-tick\)/);
	assert.match(strip, /var\(--rb-deck-link-tick-top\)/);
});

test('every linked panel reads the tick token with no private fallback', () => {
	// A fallback would keep one panel lit when the shared token is renamed or
	// dropped, while the deck and mixer go blank: the link would split.
	const files = [
		'../../src/lib/components/rb/Deck.svelte',
		'../../src/lib/components/rb/mixer/ChannelStrip.svelte',
		'../../src/lib/components/rb/wave/WaveRow.chrome.css',
		'../../src/lib/components/rb/browser/TrackTable.svelte'
	];
	for (const f of files) {
		const src = readFileSync(new URL(f, import.meta.url), 'utf8');
		assert.match(src, /var\(--rb-deck-link-tick(-top)?\)/, `${f} uses the tick token`);
		assert.doesNotMatch(src, /var\(--rb-deck-link-tick(-top)?\s*,/, `${f} has no tick fallback`);
	}
});
