import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Dropping tracks onto a tag in the Taglists tree (LIBTAG-9668).
 *
 * [if] a type header or the untyped header accepts a drop [then] LIBTAG-9668 is broken.
 * [if] a track that already holds the tag is written again [then] LIBTAG-9668 is broken.
 * [if] a drop of only tagged tracks says anything but "Already tagged" [then] LIBTAG-9668 is broken.
 */

let drop;

before(async () => {
	drop = await loadTypeScriptModule('src/lib/components/rb/browser/tag-drop.ts');
});

describe('tag-drop', () => {
	it('accepts a drop on a tag row only', () => {
		assert.equal(drop.tagRowAcceptsDrop({ kind: 'tag', name: 'piano' }), true);
		assert.equal(drop.tagRowAcceptsDrop({ kind: 'type-head' }), false);
		assert.equal(drop.tagRowAcceptsDrop({ kind: 'untyped-head' }), false);
	});

	it('skips tracks that already hold the tag, by tag identity', () => {
		const assignments = { a: ['Piano'], b: ['dark'], c: [] };
		assert.deepEqual(drop.idsNeedingTag(['a', 'b', 'c', 'd'], 'piano', assignments), [
			'b',
			'c',
			'd'
		]);
	});

	it('writes each dropped track once', () => {
		assert.deepEqual(drop.idsNeedingTag(['b', 'b', 'c'], 'piano', {}), ['b', 'c']);
	});

	it('returns nothing when every dropped track holds the tag', () => {
		assert.deepEqual(drop.idsNeedingTag(['a'], 'piano', { a: ['piano'] }), []);
	});

	it('words the toast for a write and for a no-op', () => {
		assert.equal(drop.tagDropToast(3, 'piano'), 'Tagged 3 tracks #piano. Cmd+Z undoes it.');
		assert.equal(drop.tagDropToast(1, 'piano'), 'Tagged 1 track #piano. Cmd+Z undoes it.');
		assert.equal(drop.tagDropToast(0, 'piano'), 'Already tagged');
	});
});
