/**
 * KEY-CELL-01 (the maintainer, Wed 4 Nov 2026: "NO STATUS WORDS IN THE KEY/BPM CELLS").
 * A failed or missing key or BPM leaves its cell empty; the reason is the hover.
 *
 * Regression lines:
 * - if the key status span renders a word then the key cell shows a status word again
 * - if the BPM status span renders a word then the BPM cell shows a status word again
 * - if the key hover card loses its note for an empty key cell then the reason is nowhere on hover
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const src = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)),
	'utf8'
);

test('the empty key cell renders no status word', () => {
	assert.match(src, /<span class="key-status" aria-hidden="true"><\/span>/);
});

test('the empty BPM cell renders no status word and keeps the reason on its hover', () => {
	assert.match(src, /<span class="bpm-status" title=\{bpmCellTitle\(row\)\} aria-hidden="true"><\/span>/);
});

test('an empty key cell explains itself on the key hover card note', () => {
	assert.match(src, /class:key-inert=\{keyCellInert\(row\) && !keyEdited\}/);
	assert.match(src, /note=\{keyHoverNote\(keyHover\.row\)\}/);
	assert.match(src, /\[\s*keyCellInert\(row\) \? keyCellTitle\(row\) : null,\s*clashEdges\.get\(row\.stable_id\) \?\? null,?\s*\]/);
});
