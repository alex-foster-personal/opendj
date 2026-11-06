/**
 * Pin c560a53603d4 (issue #4089): artwork on the left of each main waveform
 * beside the track name; a long title truncates and scrubs on hover.
 * DECKUX-40 (Tue 6 Oct 2026) moved the name from below the artwork to its
 * right, with the artwork at full row height.
 *
 * Regression lines:
 * - if WaveRow mounts the waveform column before the gutter then the artwork
 *   moves to the right of the waveform
 * - if WaveGutter stops mounting WaveTrackSummary then the artwork and name vanish
 *   (the markup lives in the props-only WaveGutterView / WaveTrackSummaryView;
 *   the containers keep the store reads and mount them)
 * - if WaveTrackSummary renders the name before the artwork, or stacks them in
 *   a column, then the name is no longer right of the artwork
 * - if the hover scrub hooks go then a truncated title cannot be read
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const WAVE = fileURLToPath(new URL('../../src/lib/components/rb/wave', import.meta.url));
const ROW = readFileSync(`${WAVE}/WaveRow.svelte`, 'utf8');
const GUTTER = readFileSync(`${WAVE}/WaveGutter.svelte`, 'utf8');
const GUTTER_VIEW = readFileSync(`${WAVE}/WaveGutterView.svelte`, 'utf8');
const SUMMARY = readFileSync(`${WAVE}/WaveTrackSummary.svelte`, 'utf8');
const SUMMARY_VIEW = readFileSync(`${WAVE}/WaveTrackSummaryView.svelte`, 'utf8');

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
		at(ROW, '<WaveGutter {deck} {deckId} />', 'WaveRow') <
			at(ROW, '<div class="wave-col">', 'WaveRow'),
		'the gutter (artwork column) must render before the waveform column'
	);
});

test('pin c560a53603d4: WaveGutter mounts WaveTrackSummary after the deck number', () => {
	assert.match(GUTTER, /import WaveTrackSummary from '\.\/WaveTrackSummary\.svelte'/);
	assert.match(GUTTER, /\{#snippet summary\(\)\}<WaveTrackSummary \{deck\} \/>\{\/snippet\}/);
	assert.ok(
		at(GUTTER_VIEW, 'class="deck-num"', 'WaveGutterView') <
			at(GUTTER_VIEW, '{@render summary()}', 'WaveGutterView'),
		'the deck number must come before the summary'
	);
});

test('pin c560a53603d4: WaveTrackSummary puts the artwork LEFT of the track name', () => {
	assert.match(SUMMARY, /<WaveTrackSummaryView\b/);
	assert.match(SUMMARY_VIEW, /<div class="wave-track-summary">/);
	const block = /\.wave-track-summary\s*\{([^}]*)\}/.exec(SUMMARY_VIEW);
	assert.ok(block, 'WaveTrackSummary must style .wave-track-summary');
	assert.match(block[1], /display:\s*flex/);
	assert.doesNotMatch(block[1], /flex-direction:\s*column/, 'a column would put the name below the art');
	assert.match(SUMMARY, /deckArtworkUrl\(deck\.stable_id, 's'\)/);
	assert.ok(
		at(SUMMARY_VIEW, 'class="wave-art"', 'WaveTrackSummaryView') <
			at(SUMMARY_VIEW, 'class="wave-track-name"', 'WaveTrackSummaryView'),
		'artwork markup must come before the name'
	);
});

test('pin c560a53603d4: a long track name truncates and scrubs on hover', () => {
	assert.match(SUMMARY_VIEW, /onpointerenter=\{startTrackNameScrub\}/);
	assert.match(SUMMARY_VIEW, /onpointerleave=\{stopTrackNameScrub\}/);
	assert.match(SUMMARY_VIEW, /@keyframes track-name-scrub/);
	assert.match(SUMMARY_VIEW, /title=\{fullTitle\}/, 'the full name stays reachable as a tooltip');
});
