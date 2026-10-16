// LIBTREE-01/02: playlist tree order, the pure half (drag move, Sort A-Z, repaint).
// The writer's concurrency and rollback run against the real route in
// playlist-tree-order-route.test.mjs.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;

before(async () => {
	m = await loadTypeScriptModule('src/lib/components/rb/browser/playlist-tree-order.ts');
});

const pl = (playlist_id, name, seq) => ({ playlist_id, name, seq });

test('tree order: seq first, then no-seq rows by name', () => {
	const rows = [pl('c', 'Zed', null), pl('a', 'Beta', 1), pl('b', 'Alpha', 0), pl('d', 'Alpha2', undefined)];
	assert.deepEqual(m.treeOrderIds(rows), ['b', 'a', 'd', 'c']);
});

test('move before and after a target', () => {
	const order = ['a', 'b', 'c', 'd'];
	assert.deepEqual(m.movePlaylistInOrder(order, 'd', 'b', 'before'), ['a', 'd', 'b', 'c']);
	assert.deepEqual(m.movePlaylistInOrder(order, 'a', 'c', 'after'), ['b', 'c', 'a', 'd']);
	assert.deepEqual(m.movePlaylistInOrder(order, 'a', 'd', 'after'), ['b', 'c', 'd', 'a']);
});

test('a drop that changes nothing writes nothing', () => {
	const order = ['a', 'b', 'c'];
	assert.equal(m.movePlaylistInOrder(order, 'b', 'b', 'before'), null);
	// b already sits right after a and right before c.
	assert.equal(m.movePlaylistInOrder(order, 'b', 'a', 'after'), null);
	assert.equal(m.movePlaylistInOrder(order, 'b', 'c', 'before'), null);
	assert.equal(m.movePlaylistInOrder(order, 'x', 'a', 'before'), null);
	assert.equal(m.movePlaylistInOrder(order, 'a', 'x', 'before'), null);
});

test('Sort A-Z is case-insensitive and numeric-aware, over every playlist', () => {
	const rows = [pl('1', 'set 10', 0), pl('2', 'Set 2', 1), pl('3', 'apple', 2), pl('4', 'Banana', null)];
	assert.deepEqual(m.nameSortedIds(rows), ['3', '4', '2', '1']);
});

test('repaint restamps seq from the order and shifts the rest after it', () => {
	const rows = [pl('a', 'A', 5), pl('b', 'B', 0), pl('c', 'C', null)];
	const out = m.withTreeOrder(rows, ['b', 'a']);
	assert.deepEqual(
		out.map((r) => [r.playlist_id, r.seq]),
		[
			['a', 1],
			['b', 0],
			['c', null]
		]
	);
	// The tree's own sort over the repainted rows gives the written order.
	assert.deepEqual(m.treeOrderIds(out), ['b', 'a', 'c']);
	// The input is not mutated (Svelte state is replaced, not edited in place).
	assert.equal(rows[0].seq, 5);
});

test('drop place splits a row at its middle', () => {
	assert.equal(m.dropPlaceAt(104, 100, 20), 'before');
	assert.equal(m.dropPlaceAt(110, 100, 20), 'after');
	assert.equal(m.dropPlaceAt(119, 100, 20), 'after');
});

function deferred() {
	let resolve;
	let reject;
	const promise = new Promise((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

test('BrowserPanel wires the writer to the real putPlaylistTreeOrder', () => {
	const panel = readFileSync(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url), 'utf8');
	assert.match(panel, /createTreeOrderWriter\(\{[\s\S]*?put: putPlaylistTreeOrder,/);
});

test('an older playlist re-read that finishes last does not repaint the older order', async () => {
	const reads = [];
	const applied = [];
	const read = m.createLatestRead(() => {
		const g = deferred();
		reads.push(g);
		return g.promise;
	});
	// Different callers (refresh, sweep, create) share one sequence.
	const older = read((rows) => applied.push(['sweep', rows]));
	const newer = read((rows) => applied.push(['refresh', rows]));
	reads[1].resolve(['c', 'a', 'b']);
	assert.equal(await newer, true);
	reads[0].resolve(['b', 'a', 'c']);
	assert.equal(await older, false);
	assert.deepEqual(applied, [['refresh', ['c', 'a', 'b']]], 'the stale GET is dropped');
	const later = read((rows) => applied.push(['create', rows]));
	reads[2].resolve(['a']);
	assert.equal(await later, true);
	assert.deepEqual(applied.at(-1), ['create', ['a']], 'a later read still applies');
});

test('BrowserPanel routes playlist re-reads through the latest-read guard', () => {
	const panel = readFileSync(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url), 'utf8');
	assert.match(panel, /createLatestRead\(listPlaylistsHydrated\);/);
	assert.match(panel, /async function _refreshPlaylists\(\)[^}]*?await _readPlaylists\(/);
	// Codex P2 on #6186: no playlist read bypasses the guard (sweep, create).
	assert.doesNotMatch(panel, /await listPlaylistsHydrated\(/);
});

test('withSeqFrom rolls back only seq and keeps names, new rows and deletions', () => {
	const baseline = new Map([['a', 0], ['b', 1], ['gone', 2]]);
	const rows = [
		{ playlist_id: 'b', name: 'B renamed', seq: 0 },
		{ playlist_id: 'a', name: 'A', seq: 1 },
		{ playlist_id: 'new', name: 'New', seq: null }
	];
	assert.deepEqual(m.withSeqFrom(rows, baseline), [
		{ playlist_id: 'b', name: 'B renamed', seq: 1 },
		{ playlist_id: 'a', name: 'A', seq: 0 },
		{ playlist_id: 'new', name: 'New', seq: null }
	]);
});
