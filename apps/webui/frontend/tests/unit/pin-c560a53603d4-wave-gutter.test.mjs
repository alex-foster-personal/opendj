/**
 * Pin c560a53603d4 (issue #4089): artwork on the left of each main waveform
 * with the track name below it, in one shared column; a long title truncates
 * and scrubs on hover. Already built on main - this test proves it and keeps it.
 *
 * Regression lines:
 * - if WaveRow mounts the waveform column before the gutter then the artwork
 *   moves to the right of the waveform
 * - if WaveGutter stops mounting WaveTrackSummary then the artwork and name vanish
 * - if WaveTrackSummary renders the name before the artwork, or lays them out in
 *   a row, then the name is no longer below the artwork
 * - if the hover scrub hooks go then a truncated title cannot be read
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const WAVE = fileURLToPath(new URL('../../src/lib/components/rb/wave', import.meta.url));
const ROW = readFileSync(`${WAVE}/WaveRow.svelte`, 'utf8');
const GUTTER = readFileSync(`${WAVE}/WaveGutter.svelte`, 'utf8');
const SUMMARY = readFileSync(`${WAVE}/WaveTrackSummary.svelte`, 'utf8');

/** Index of `needle` in `source`, failing loudly when absent so an ordering
 * check can never pass by comparing two -1s. */
function at(source, needle, label) {
	const i = source.indexOf(needle);
	assert.ok(i >= 0, `${label}: '${needle}' not found`);
	return i;
}

test('pin c560a53603d4: the gutter sits LEFT of the waveform column in WaveRow', () => {
	assert.match(ROW, /import WaveGutter from '\.\/WaveGutter\.svelte'/);
	assert.ok(
		at(ROW, '<WaveGutter {deck} {deckId} {barsLabel} />', 'WaveRow') <
			at(ROW, '<div class="wave-col">', 'WaveRow'),
		'the gutter (artwork column) must render before the waveform column'
	);
});

test('pin c560a53603d4: WaveGutter mounts WaveTrackSummary beside the deck indicators', () => {
	assert.match(GUTTER, /import WaveTrackSummary from '\.\/WaveTrackSummary\.svelte'/);
	assert.match(GUTTER, /<WaveTrackSummary \{deck\} \/>/);
	assert.match(GUTTER, /class="deck-num"/);
});

test('pin c560a53603d4: WaveTrackSummary stacks the artwork ABOVE the track name', () => {
	assert.match(SUMMARY, /<div class="wave-track-summary">/);
	assert.match(SUMMARY, /\.wave-track-summary\s*\{[^}]*flex-direction:\s*column/);
	assert.match(SUMMARY, /deckArtworkUrl\(deck\.stable_id, 's'\)/);
	assert.ok(
		at(SUMMARY, 'class="wave-art"', 'WaveTrackSummary') <
			at(SUMMARY, 'class="wave-track-name"', 'WaveTrackSummary'),
		'artwork markup must come before the name in a column layout'
	);
});

test('pin c560a53603d4: a long track name truncates and scrubs on hover', () => {
	assert.match(SUMMARY, /onpointerenter=\{startTrackNameScrub\}/);
	assert.match(SUMMARY, /onpointerleave=\{stopTrackNameScrub\}/);
	assert.match(SUMMARY, /@keyframes track-name-scrub/);
	assert.match(SUMMARY, /title=\{trackName\}/, 'the full name stays reachable as a tooltip');
});
