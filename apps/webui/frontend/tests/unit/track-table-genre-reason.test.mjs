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
