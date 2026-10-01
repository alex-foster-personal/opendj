/**
 * STANDALONE-05 + LIBUX-36: a missing genre is a blank cell, and the reason
 * it is missing is on hover, never printed into the data cell.
 *
 * the maintainer, Thu 1 Oct 2026, on the demon-llama previews: Genre showed
 * "no genre tag in..." (truncated prose) instead of being blank. The reason
 * is still a fact worth keeping (a missing tag reader and an untagged file
 * are different things, STANDALONE-05), so it moves to the cell's hover
 * title; the minor-issue square in the cloud column also still carries it.
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

test('Genre cell exposes genre_reason as the hover title', () => {
	const cell = genreCell();
	const td = cell.slice(0, cell.indexOf('>'));
	assert.match(td, /title=\{[^}]*row\.genre_reason/, 'genre reason is not the cell hover title');
});

test('Genre cell never prints the reason as cell text', () => {
	const cell = genreCell();
	const body = cell.slice(cell.indexOf('>') + 1);
	assert.doesNotMatch(body, /\{row\.genre_reason\}/, 'genre reason is rendered as visible text');
	assert.doesNotMatch(body, /genre-reason/, 'a genre-reason element still renders');
});

test('Genre reason is not rendered as a clickable genre filter tag', () => {
	const cell = genreCell();
	assert.doesNotMatch(cell, /onGenreTagClick\(e,\s*row\.genre_reason/);
});
