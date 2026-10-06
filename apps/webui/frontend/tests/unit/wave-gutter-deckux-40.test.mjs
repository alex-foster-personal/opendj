/**
 * DECKUX-40 (the maintainer, Tue 6 Oct 2026, W29): the waveform-row gutter is a large
 * brand-face deck number, full-height artwork, and a title that fades out
 * over the live key and BPM. An empty deck shows only its number, missing
 * artwork is empty space, there is no bars countdown and no row divider.
 *
 * What the browser paints is measured by
 * tests/e2e/performance-wave-title-emphasis.spec.ts and the before/after
 * evidence on the PR; this file holds the pure title rule and the structural
 * lines a refactor could quietly undo.
 *
 * Regression lines:
 * - if the Mixed In Key prefix is not stripped then "9A - 7 - Shiver" spends
 *   a third of the gutter repeating the key the meta line already shows
 * - if a prefix-only title is stripped to nothing then the row reads blank
 * - if the bars countdown or the row divider comes back then the design the maintainer
 *   signed off is undone
 * - if the live key or BPM reads deck.position_ms directly then the grid
 *   lookup runs every frame in four gutters
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const WAVE = fileURLToPath(new URL('../../src/lib/components/rb/wave', import.meta.url));
const read = (name) => readFileSync(`${WAVE}/${name}`, 'utf8');

let titles;
before(async () => {
	titles = await loadTypeScriptModule('src/lib/components/rb/wave/track-title-display.ts');
});

// REQ: DECKUX-40
test('the gutter title drops Mixed In Key key/energy prefixes', () => {
	const cases = [
		['9A - 7 - Shiver', 'Shiver'],
		['4A - 6 - Clouds', 'Clouds'],
		['12B - Title Only Key', 'Title Only Key'],
		['Am - 5 - Minor Notation', 'Minor Notation'],
		['F#m - 8 - Sharp Minor', 'Sharp Minor']
	];
	for (const [input, expected] of cases) {
		assert.equal(titles.gutterTrackTitle(input), expected, input);
	}
});

// REQ: DECKUX-40
test('a title without a prefix, or that is only a prefix, is left whole', () => {
	for (const input of ['Verdad', "You've Got The Love", '2 - 4 - 6 Steps', 'A - Side']) {
		assert.equal(titles.gutterTrackTitle(input), input, input);
	}
	assert.equal(titles.gutterTrackTitle('8A - '), '8A - ', 'never strip a title to nothing');
});

// REQ: DECKUX-40
test('an empty deck mounts no track summary, only its number', () => {
	const gutter = read('WaveGutter.svelte');
	assert.match(gutter, /\{#if loaded\}<WaveTrackSummary \{deck\} \/>\{\/if\}/);
	assert.match(gutter, /font-family:\s*var\(--rb-font-brand\)/, 'the deck number uses the brand face');
});

// REQ: DECKUX-40
test('there is no bars countdown and no row divider', () => {
	assert.doesNotMatch(read('WaveGutter.svelte'), /class="bars"|barsLabel|barsTitle/);
	assert.doesNotMatch(read('WaveRow.svelte'), /barsLabel|barsToNextCueLabel/);
	assert.doesNotMatch(read('wave-math.ts'), /export function barsToNextCueLabel/);
	const row = /^\.rb-waverow\s*\{([^}]*)\}/m.exec(read('WaveRow.chrome.css'));
	assert.ok(row, 'WaveRow.chrome.css must still style .rb-waverow');
	assert.doesNotMatch(row[1], /border-bottom/);
});

// REQ: DECKUX-40
test('live key and BPM read a one-second position bucket, not the per-frame position', () => {
	const summary = read('WaveTrackSummary.svelte');
	assert.match(summary, /const positionBucketSec = \$derived\(Math\.floor\(/);
	const live = summary.slice(summary.indexOf('const liveBpm'), summary.indexOf('const keyColor'));
	assert.ok(live.length > 0, 'liveBpm and liveKey must be declared before keyColor');
	assert.doesNotMatch(live, /deck\.position_ms/);
	assert.match(live, /positionBucketSec/);
});
