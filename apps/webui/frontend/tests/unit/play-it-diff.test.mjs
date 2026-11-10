import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/play-it-diff.ts');
});

test('summarizeReorder reports zero moves for an identical order', () => {
	const summary = mod.summarizeReorder(['a', 'b', 'c'], ['a', 'b', 'c']);
	assert.deepEqual(summary, { movedCount: 0, unchanged: true });
});

test('summarizeReorder counts positions that changed', () => {
	const summary = mod.summarizeReorder(['a', 'b', 'c'], ['c', 'b', 'a']);
	assert.equal(summary.movedCount, 2);
	assert.equal(summary.unchanged, false);
});

test('summarizeReorder handles empty playlists', () => {
	const summary = mod.summarizeReorder([], []);
	assert.deepEqual(summary, { movedCount: 0, unchanged: true });
});

test('formatUnscoredEdges is null when the count is zero', () => {
	assert.equal(mod.formatUnscoredEdges(0, 39, ['key']), null);
});

test('formatUnscoredEdges names a single missing term', () => {
	assert.equal(
		mod.formatUnscoredEdges(3, 39, ['key', 'key']),
		'3 of 39 edges unscored (key missing)'
	);
});

test('formatUnscoredEdges lists measured-term names in a stable order', () => {
	assert.equal(
		mod.formatUnscoredEdges(2, 10, ['artist', 'energy', 'bpm']),
		'2 of 10 edges unscored (bpm missing, energy missing, artist missing)'
	);
});
