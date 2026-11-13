// requirement: EDGESAMP-13997
/**
 * Issue #11102 (S7/S7b): sets, the pages of slots one edge shows ((private path) 7.4).
 *
 * Regression lines:
 * - if 45 slots are split by 20 then sets are 20, 20, 5 named Set 1 to Set 3
 * - if a capture is dropped into a full set then its last slot moves to the next set, none lost
 * - if a capture is dropped on an empty slot then only that slot changes
 * - if a set is flipped past either end then it wraps
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let sets;
before(async () => {
	sets = await loadTypeScriptModule('src/lib/player/sampler/sampler-sets.ts');
});

const range = (n) => Array.from({ length: n }, (_, i) => i);

test('45 slots split by 20 make sets of 20, 20 and 5', () => {
	/** [if] 45 slots are split by 20 with a name for set-2 [then] sizes are 20, 20, 5 and set-2 has that name, [else stop]. */
	const s = sets.buildSets(range(45), 20, { 'set-2': { name: 'Warmup', description: 'd' } });
	assert.deepEqual(
		s.map((x) => x.items.length),
		[20, 20, 5]
	);
	assert.deepEqual(
		s.map((x) => x.name),
		['Set 1', 'Warmup', 'Set 3']
	);
	assert.throws(() => sets.buildSets(range(3), 0), /perSet must be positive/);
});

test('a drop into a full set pushes its last slot to the next set', () => {
	/** [if] a capture is dropped on slot 3 of a full set [then] old slot 20 opens set 2 and no slot is lost, [else stop]. */
	const s = sets.buildSets(range(40), 20);
	sets.insertIntoSet(s, 0, 2, 'new', 20);
	assert.equal(s[0].items[2], 'new');
	assert.equal(s[0].items.length, 20);
	assert.equal(s[1].items[0], 19);
	assert.equal(s.flatMap((x) => x.items).length, 41);
	assert.equal(s[2].items[0], 39);
});

test('a drop on an empty slot fills it and moves nothing', () => {
	/** [if] a capture is dropped on an empty slot [then] it fills that slot and the rest stay put, [else stop]. */
	const s = [{ id: 'set-1', name: 'Set 1', description: '', items: [1, null, 3] }];
	sets.insertIntoSet(s, 0, 1, 'new', 20);
	assert.deepEqual(s[0].items, [1, 'new', 3]);
});

test('a drop before a hole closes the hole instead of shifting the set on', () => {
	/** [if] a capture lands on slot 1 of a set with a later empty slot [then] the hole is used and set 2 is untouched, [else stop]. */
	const s = [
		{ id: 'set-1', name: 'Set 1', description: '', items: [1, 2, null, 4] },
		{ id: 'set-2', name: 'Set 2', description: '', items: [5] }
	];
	sets.insertIntoSet(s, 0, 0, 'new', 4);
	assert.deepEqual(s[0].items, ['new', 1, 2, 4]);
	assert.deepEqual(s[1].items, [5]);
});

test('paging wraps past either end', () => {
	/** [if] paging -1 from set 0 or +1 from the last of 3 [then] it lands on 2 and 0, and 0 with no sets, [else stop]. */
	assert.equal(sets.pageWrap(0, -1, 3), 2);
	assert.equal(sets.pageWrap(2, 1, 3), 0);
	assert.equal(sets.pageWrap(0, 1, 0), 0);
});
