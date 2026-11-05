/**
 * KEY-GUESS-01: the library key cell is a hover-card host, so the dimmed
 * "8A?" guess explains itself on that card, never on a title= of its own.
 *
 * Regression lines:
 * - if the key-guess span carries title= then the cell draws two explainers
 * - if keyCellTitle drops the guess line then the hover card never says it is a guess
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const TABLE = fileURLToPath(new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url));
const src = readFileSync(TABLE, 'utf8');

test('the dimmed guess span has no title of its own', () => {
	assert.match(src, /<span class="key-guess">/);
	assert.doesNotMatch(src, /<span class="key-guess"[^>]*title=/);
});

test('the key hover card note carries the best guess and its confidence', () => {
	const fn = src.slice(src.indexOf('function keyCellTitle'), src.indexOf('function keyCellTitle') + 1200);
	assert.match(fn, /row\.key_guess\)[\s\S]*Best guess \$\{row\.key_guess\} \(ambiguous key, confidence \$\{Math\.round\(\(row\.key_guess_confidence \?\? 0\) \* 100\)\}%\)/);
	assert.match(src, /note=\{keyHoverNote\(keyHover\.row\)\}/);
	assert.match(src, /\[keyCellInert\(row\) \? keyCellTitle\(row\) : null, clashEdges\.get\(row\.stable_id\) \?\? null\]/);
});
