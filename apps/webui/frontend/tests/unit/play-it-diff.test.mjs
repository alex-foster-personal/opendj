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
