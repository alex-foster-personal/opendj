/**
 * Pin 4de63478782c: deck header shows the track rating (same viewability
 * rules as library: dimmed on no rating, clickable to set), plus an empty
 * colour dot after it. SCOPE for this packet: reuse RatingStars (the
 * library's own control), and render only the dot (empty circle until set)
 * -- the library colour COLUMN, its picker popover, and the multi-colour
 * 4-circle grid are out of scope (a separate feature).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const header = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/deck/DeckHeader.svelte', import.meta.url)),
	'utf8'
);

function metaBlock() {
	const start = header.indexOf('class="meta"');
	const artistIdx = header.indexOf('class="artist"', start);
	assert.notEqual(artistIdx, -1, 'no artist line in DeckHeader meta block');
	const metaCloseIdx = header.indexOf('</div>', artistIdx);
	return header.slice(start, metaCloseIdx);
}

test('DeckHeader imports and reuses the library RatingStars control (no second implementation)', () => {
	assert.match(header, /import RatingStars from '\.\.\/browser\/RatingStars\.svelte';/);
	assert.match(header, /import\s*\{[^}]*rateDeckTrack[^}]*\}\s*from '\$lib\/rb\/audio-engine\.svelte';/s);
});

test('rating sits under the artist name, wired to the deck rating + rateDeckTrack', () => {
	const meta = metaBlock();
	assert.match(meta, /<RatingStars\b[^>]*rating=\{deck\.rating\}/s);
	assert.match(meta, /onrate=\{[^}]*rateDeckTrack\(deckId,/s);
});

test('an empty colour dot renders after the rating -- no picker, no multi-colour grid', () => {
	const meta = metaBlock();
	assert.match(meta, /class="color-dot"/, 'no colour dot after the rating');
	// out of scope for this packet: a click-to-open colour picker popover or
	// a 4-circle multi-tag grid must not be present here.
	assert.doesNotMatch(meta, /color-picker|colour-picker/i);
	assert.doesNotMatch(meta, /four-circle|four-dot|multi-color-grid|multi-colour-grid/i);
});
