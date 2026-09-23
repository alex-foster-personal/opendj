/**
 * STANDALONE-05: TrackTable renders genre_reason when no genre tags exist.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const table = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
	'utf8'
);

function genreCell() {
	const start = table.indexOf('class="c-genre"');
	assert.notEqual(start, -1, 'no Genre cell in TrackTable');
	const open = table.lastIndexOf('<td', start);
	const close = table.indexOf('</td>', start);
	return table.slice(open, close);
}

test('Genre cell defines a genre-reason branch for empty genre tags', () => {
	const cell = genreCell();
	assert.match(cell, /row\.genre_reason/, 'cell does not read row.genre_reason');
	assert.match(cell, /class="genre-reason"/, 'no genre-reason element');
	assert.match(cell, /title=\{row\.genre_reason\}/, 'genre reason is not exposed as hover title');
});

test('Genre reason branch is not rendered as a clickable genre filter tag', () => {
	const cell = genreCell();
	const reasonIdx = cell.indexOf('genre-reason');
	const tagIdx = cell.indexOf('genre-tag', reasonIdx);
	assert.equal(tagIdx, -1, 'genre-reason must not use the genre-tag filter button');
});

function genreReasonRule() {
	const start = table.indexOf('.genre-reason {');
	assert.notEqual(start, -1, 'no .genre-reason style rule in TrackTable');
	return table.slice(start, table.indexOf('}', start));
}

test('Genre reason keeps the fixed row height: it must not wrap inside the cell', () => {
	// tbody rows are `height: var(--tt-row-h)` because the virtualization
	// window math needs a constant height. A wrapping reason grew every
	// state-only row from 22.5px to 25px, which moved the right-click point
	// and pushed the Show in playlists popover past the viewport edge
	// (track-playlists.spec.ts red 3 of 3 on CI). The td already clips with
	// nowrap + ellipsis, and the full text is the hover title.
	const rule = genreReasonRule();
	assert.doesNotMatch(rule, /white-space\s*:\s*(normal|pre-wrap|pre-line|break-spaces)/, 'genre-reason must not override the td nowrap');
});
