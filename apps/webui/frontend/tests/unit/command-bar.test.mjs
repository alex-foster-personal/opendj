// requirement: CMDK-01
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/command-bar.ts');
});

function row(stable_id, title, artist, extra = {}) {
	return {
		stable_id,
		title,
		artist,
		genre: null,
		key: null,
		bpm: null,
		file_exists: true,
		is_streaming: false,
		...extra
	};
}

const ROWS = [
	row('a', 'One More Time', 'Daft Punk', { genre: 'House', key: '8A' }),
	row('b', 'Around the World', 'Daft Punk'),
	row('c', 'Strings of Life', 'Rhythim Is Rhythim', { genre: 'Techno' })
];

test('every token must match, in any order and any field', () => {
	const hay = ROWS.map(mod.rowHaystack);
	assert.deepEqual(
		mod.matchRows(ROWS, hay, 'punk one').rows.map((r) => r.stable_id),
		['a']
	);
	assert.deepEqual(
		mod.matchRows(ROWS, hay, 'TECHNO').rows.map((r) => r.stable_id),
		['c']
	);
	// The overshoot to guard: matching ANY token would return both Daft Punk rows.
	assert.equal(mod.matchRows(ROWS, hay, 'daft strings').total, 0);
});

test('an empty query lists the playlist as it stands', () => {
	const hay = ROWS.map(mod.rowHaystack);
	const result = mod.matchRows(ROWS, hay, '   ');
	assert.deepEqual(result.rows.map((r) => r.stable_id), ['a', 'b', 'c']);
	assert.equal(result.total, 3);
});

test('the list is cut at the limit but the total counts every match', () => {
	const many = Array.from({ length: 120 }, (_, i) => row(`r${i}`, `Track ${i}`, 'Same'));
	const result = mod.matchRows(many, many.map(mod.rowHaystack), 'same', 50);
	assert.equal(result.rows.length, 50);
	assert.equal(result.total, 120);
});

test('haystacks out of step with rows fail loudly', () => {
	assert.throws(() => mod.matchRows(ROWS, [], 'x'), /0 haystacks for 3 rows/);
});

test('default deck: lowest empty, else lowest non-master', () => {
	const d = (id, loaded, is_master = false) => ({ id, loaded, is_master });
	assert.equal(mod.defaultTargetDeck([d(1, true), d(2, false), d(3, false), d(4, false)]), 2);
	assert.equal(mod.defaultTargetDeck([d(1, true, true), d(2, true), d(3, true), d(4, true)]), 2);
	assert.equal(mod.defaultTargetDeck([d(1, true), d(2, true), d(3, true), d(4, true)]), 1);
});

test('deck and row stepping stop at the ends', () => {
	assert.equal(mod.stepDeck([1, 2, 3, 4], 4, 1), 4);
	assert.equal(mod.stepDeck([1, 2, 3, 4], 1, -1), 1);
	assert.equal(mod.stepDeck([1, 2, 3, 4], 2, 1), 3);
	assert.equal(mod.stepSelection(0, 5, -1), 0);
	assert.equal(mod.stepSelection(4, 5, 1), 4);
	assert.equal(mod.stepSelection(0, 0, 1), 0);
});

test('the bar loads through the browser deck-load path, not its own', () => {
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	assert.match(panel, /<CommandBar rows=\{pane\.rows\}[^>]*load=\{_commandBarLoad\}/);
	assert.match(panel, /async function _commandBarLoad[\s\S]{0,200}await _loadOntoDeck\(row, deck\)/);
	const bar = readFileSync(`${SRC}/lib/components/rb/CommandBar.svelte`, 'utf8');
	assert.doesNotMatch(bar, /dispatchPerformanceCommand|runPerformanceCommandFromUi/);
});
