// requirement: a duet line (two lyrics at one start_ms) renders readably on
// the waveform lyrics lane and in the line lyrics panel (LYRICS-09).
//
// The parser accepts equal stamps since LYRICS-09, so the components must too.
// Before this fix LyricsLane keyed its each-block by start_ms (a duplicate key)
// and drew both lines as absolutely positioned spans at the same `left`, one
// on top of the other.
//
// [if] two lines share start_ms [then] the lane renders ONE entry carrying
//   both texts, at one position, marked active once the playhead reaches it
// [if] timestamps are distinct [then] each line keeps its own entry (control:
//   grouping must not merge neighbors that merely sit close together)
// [if] the line panel gets two identical lines at one stamp [then] its rows
//   are keyed by position, so the keys cannot collide
//
// Rendered with svelte's own SSR renderer (load-svelte-ssr.mjs): this proves
// the markup, not the pixels.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as LyricsLane } from '$lib/components/rb/wave/LyricsLane.svelte';",
	"export { lyricLaneGroups } from '$lib/components/rb/wave/lyrics-lane';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

function laneSpans(lines, positionMs) {
	const html = mod.render(mod.LyricsLane, {
		props: { lyrics: { lines }, loadError: null, positionMs, pitch: 1 }
	}).body;
	return [...html.matchAll(/<span([^>]*)class="lyric-line[^"]*"[^>]*>([^<]*)<\/span>/g)].map(
		(match) => ({ tag: match[0], text: match[2] })
	);
}

test('a duet renders as one lane entry carrying both lines', () => {
	const spans = laneSpans(
		[
			{ start_ms: 1_000, text: 'Lead' },
			{ start_ms: 1_000, text: 'Backing' },
			{ start_ms: 2_500, text: 'Next' }
		],
		1_200
	);
	assert.deepEqual(
		spans.map((span) => span.text),
		['Lead / Backing', 'Next']
	);
	assert.match(spans[0].tag, /aria-current="true"/);
	assert.doesNotMatch(spans[1].tag, /aria-current/);
});

test('control: distinct stamps keep one entry each, however close', () => {
	const spans = laneSpans(
		[
			{ start_ms: 1_000, text: 'One' },
			{ start_ms: 1_001, text: 'Two' }
		],
		1_000
	);
	assert.deepEqual(
		spans.map((span) => span.text),
		['One', 'Two']
	);
	assert.match(spans[0].tag, /aria-current="true"/);
});

test('lyricLaneGroups keeps the index range the active-line lookup reports', () => {
	assert.deepEqual(
		mod.lyricLaneGroups([
			{ start_ms: 0, text: 'a' },
			{ start_ms: 500, text: 'b' },
			{ start_ms: 500, text: 'c' },
			{ start_ms: 500, text: 'd' }
		]),
		[
			{ start_ms: 0, text: 'a', firstIndex: 0, lastIndex: 0 },
			{ start_ms: 500, text: 'b / c / d', firstIndex: 1, lastIndex: 3 }
		]
	);
});

test('the line panel keys rows by position, so identical duet lines cannot collide', async () => {
	// The panel fetches its own lyrics in onMount, which SSR never runs, so this
	// one is a source pin: the old key (start_ms + text) repeats for two
	// identical lines at one stamp, which the parser now accepts.
	const source = await readFile(
		new URL('../../src/lib/components/LineLyricsPanel.svelte', import.meta.url),
		'utf8'
	);
	assert.match(source, /\{#each lyrics\.lines as line, index \(index\)\}/);
	assert.doesNotMatch(source, /line\.start_ms \+ line\.text/);
});
