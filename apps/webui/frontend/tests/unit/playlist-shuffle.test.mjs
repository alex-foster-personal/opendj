/** Issue #11325: seeded weighted playlist shuffle and items:move persistence. */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let shuffle;
let menu;

before(async () => {
	shuffle = await loadTypeScriptModule('src/lib/rb/playlist-shuffle.ts');
	menu = await loadTypeScriptModule('src/lib/components/rb/browser/track-context-menu.ts');
});

test('weighted shuffle favors low Open DJ play counts at the front', () => {
	const members = [
		{ item_id: 'i-a', stable_id: 'a' },
		{ item_id: 'i-b', stable_id: 'b' }
	];
	let aFirst = 0;
	for (let seed = 1; seed <= 80; seed += 1) {
		const order = shuffle.weightedShuffleItemIds(members, (sid) => (sid === 'a' ? 0 : 40), seed);
		if (order[0] === 'i-a') aFirst += 1;
	}
	assert.ok(aFirst >= 52, `expected a first often, got ${aFirst}/80`);
});

test('reorderItemIdsViaMove applies a permutation with one-item moves', async () => {
	const calls = [];
	const move = async (_pid, etag, body) => {
		calls.push(body);
		return { etag: `"rev-${calls.length}"`, items: [] };
	};
	const result = await shuffle.reorderItemIdsViaMove(
		'pl-1',
		'"rev-0"',
		['a', 'b', 'c', 'd'],
		['c', 'a', 'd', 'b'],
		move
	);
	assert.equal(result.moves, 2);
	assert.equal(calls.length, 2);
	assert.equal(result.etag, '"rev-2"');
	assert.equal(calls[0].range_start, 'c');
	assert.equal(calls[0].before_item_id, 'a');
});

test('runPlaylistShuffle refuses when reorder is blocked', async () => {
	const toasts = [];
	await menu.runPlaylistShuffle({
		rows: [{ item_id: 'i1', stable_id: 's1' }],
		playlistId: 'pl',
		etag: '"e"',
		allowed: false,
		blockedReason: 'Sorted by title',
		move: async () => {
			throw new Error('move must not run');
		},
		fetchCounts: async () => new Map(),
		seed: () => 1,
		reload: async () => {},
		pushToast: (message, kind) => toasts.push({ message, kind })
	});
	assert.deepEqual(toasts, [{ message: 'Sorted by title', kind: 'error' }]);
});

test('runPlaylistShuffle posts a move when membership is shuffled', async () => {
	const toasts = [];
	let moved = false;
	await menu.runPlaylistShuffle({
		rows: [
			{ item_id: 'i-a', stable_id: 'a' },
			{ item_id: 'i-b', stable_id: 'b' }
		],
		playlistId: 'pl',
		etag: '"rev"',
		allowed: true,
		blockedReason: null,
		move: async () => {
			moved = true;
			return { etag: '"rev-2"', moves: 1 };
		},
		fetchCounts: async () =>
			new Map([
				['a', 0],
				['b', 0]
			]),
		seed: () => 99,
		reload: async () => {},
		pushToast: (message, kind) => toasts.push({ message, kind })
	});
	assert.equal(moved, true);
	assert.equal(toasts.length, 1);
	assert.equal(toasts[0].kind, 'info');
});
