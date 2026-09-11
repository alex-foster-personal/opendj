/**
 * Deck and wave artwork must gate <img src> on shouldFetchArtwork.
 *
 * Regression lines:
 * - if DeckHeader or WaveTrackSummary calls artworkUrl(deck.stable_id, …) unguarded then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../../', import.meta.url));
const DECK_HEADER = `${ROOT}src/lib/components/rb/deck/DeckHeader.svelte`;
const WAVE_SUMMARY = `${ROOT}src/lib/components/rb/wave/WaveTrackSummary.svelte`;

function assertArtworkGated(relPath, source) {
	assert.match(
		source,
		/shouldFetchArtwork\(deck\.stable_id\)[\s\S]*artworkUrl\(deck\.stable_id/,
		`${relPath} must call shouldFetchArtwork before artworkUrl in the derived src`
	);
	assert.doesNotMatch(
		source,
		/deck\.stable_id\s*===\s*null\s*\?\s*null\s*:\s*artworkUrl/,
		`${relPath} still sets artworkUrl from stable_id alone without shouldFetchArtwork`
	);
}

test('DeckHeader gates artworkUrl on shouldFetchArtwork', () => {
	const source = readFileSync(DECK_HEADER, 'utf8');
	assertArtworkGated('DeckHeader.svelte', source);
});

test('WaveTrackSummary gates artworkUrl on shouldFetchArtwork', () => {
	const source = readFileSync(WAVE_SUMMARY, 'utf8');
	assertArtworkGated('WaveTrackSummary.svelte', source);
});
