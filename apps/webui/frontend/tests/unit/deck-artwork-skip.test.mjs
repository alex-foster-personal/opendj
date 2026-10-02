/**
 * Decks ask for artwork with the online lookup; library rows never do.
 *
 * A loaded deck is the one place a missing cover is worth a MusicBrainz +
 * Cover Art Archive lookup (the server holds it to one request per second),
 * so DeckHeader and WaveTrackSummary request deckArtworkUrl even when the
 * listing said the track has no local artwork. A library page asking the same
 * would queue a lookup per row.
 *
 * Regression lines:
 * - if a deck artwork src skips the online lookup for a track with no local art then broken
 * - if the library row artwork src asks for the online lookup then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../../', import.meta.url));
const read = (rel) => readFileSync(`${ROOT}${rel}`, 'utf8');

for (const [name, rel, size] of [
	['DeckHeader', 'src/lib/components/rb/deck/DeckHeader.svelte', 'orig'],
	['WaveTrackSummary', 'src/lib/components/rb/wave/WaveTrackSummary.svelte', 's']
]) {
	test(`${name} requests deck artwork with the online lookup`, () => {
		const source = read(rel);
		assert.match(source, new RegExp(`deckArtworkUrl\\(deck\\.stable_id, '${size}'\\)`));
		assert.doesNotMatch(source, /shouldFetchArtwork/, `${name} must not skip a known local miss`);
		assert.doesNotMatch(source, /[^k]artworkUrl\(deck\.stable_id/, `${name} must not use the plain url`);
	});
}

test('deckArtworkUrl adds online=true to the plain artwork url', () => {
	const api = read('src/lib/rb/api-rb.ts');
	assert.match(api, /export function deckArtworkUrl[\s\S]*?\$\{artworkUrl\(stable_id, size\)\}&online=true/);
});

test('library rows never ask for the online lookup', () => {
	const table = read('src/lib/components/rb/browser/TrackTable.svelte');
	assert.match(table, /artworkUrl\(row\.stable_id, 's'\)/);
	assert.doesNotMatch(table, /deckArtworkUrl|online=true/);
});
