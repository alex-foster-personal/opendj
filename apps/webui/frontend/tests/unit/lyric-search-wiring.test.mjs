import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// Part 3 of #935 (issue #1344) wiring guards. lyric-search.test.mjs covers the
// pure debounce/gating logic; a Svelte component cannot be mounted by this
// repo's esbuild-based unit harness (see library-perf-wiring.test.mjs for the
// same constraint), so the two regressions below - both one-line reverts
// nothing else here would catch - are asserted against source instead:
//
// - if LyricSearchResults stops being mounted AFTER TrackTable in source order
//   then a later refactor could put it ahead of the primary results, which
//   the acceptance criteria for #1344 explicitly forbid
// - if `primarySettled` stops being wired to `!pane.searching` then lyric
//   results can be requested (and race to render) while the primary
//   whole-collection search is still in flight

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

test('LyricSearchResults is mounted after TrackTable, gated on the whole-collection search', () => {
	const src = source('src/lib/components/rb/BrowserPanel.svelte');

	const trackTableIndex = src.indexOf('<TrackTable');
	const lyricResultsIndex = src.indexOf('<LyricSearchResults');
	assert.ok(trackTableIndex >= 0, 'TrackTable must still be mounted');
	assert.ok(lyricResultsIndex >= 0, 'LyricSearchResults must be mounted');
	assert.ok(
		lyricResultsIndex > trackTableIndex,
		'LyricSearchResults must render AFTER TrackTable in source order - mounting it ' +
			'earlier risks a future layout change putting lyric-only hits ahead of the ' +
			'primary title/artist results #1344 requires them to follow'
	);

	assert.match(
		src,
		/<LyricSearchResults\s+[^>]*query=\{pane\.search\}/,
		'LyricSearchResults must receive the live search query'
	);
	assert.match(
		src,
		/<LyricSearchResults\s+[^>]*active=\{wholeCollectionActive\}/,
		'LyricSearchResults must only activate during the whole-collection search'
	);
	assert.match(
		src,
		/<LyricSearchResults\s+[^>]*primarySettled=\{!pane\.searching\}/,
		'if primarySettled is not wired to !pane.searching then a lyric fetch can fire ' +
			'while the primary metadata search is still in flight'
	);
});

test('LyricSearchResults renders a divider only when there are hits, never an empty placeholder', () => {
	const src = source('src/lib/components/rb/browser/LyricSearchResults.svelte');

	assert.match(
		src,
		/\{#if state\.items\.length > 0\}/,
		'an empty result must render no divider - "honest empty", not a placeholder or ' +
			'a "still indexing" guess'
	);
	assert.match(
		src,
		/role="separator"/,
		'the divider must be a labeled, identifiable element, not bare text'
	);
	assert.match(
		src,
		/controller\.update\(query, primarySettled, active\)/,
		'the component must route every prop change through the debounced, ' +
			'order-safe controller rather than fetching directly'
	);
});
