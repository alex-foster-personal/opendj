/**
 * Issue #11422: visible rows and loaded decks refresh vocals/stems after
 * stems jobs without a full library reload.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const mod = await loadTypeScriptModule('src/lib/rb/library-row-assets-refresh.ts');
const requestMod = await loadTypeScriptModule('src/lib/rb/library-row-assets-refresh-request.ts');

function row(stable_id, vocalsStatus = 'not_analyzed') {
	return {
		stable_id,
		vocals: { status: vocalsStatus, regions: [] },
		artwork_available: null,
		artwork_status: 'unresolved',
		stems: null
	};
}

test('applyRowAssetsRefresh nudges matching visible rows', () => {
	const rows = [row('a'), row('b')];
	const settled = new WeakSet();
	settled.add(rows[0]);
	const nudged = [];
	mod.applyRowAssetsRefresh(['a'], rows, settled, (ids) => nudged.push([...ids]));
	assert.deepEqual(nudged, [['a']]);
	assert.equal(settled.has(rows[0]), false);
});

test('requestLibraryRowAssetsRefresh reaches subscribers', () => {
	const seen = [];
	const off = requestMod.subscribeLibraryRowAssetsRefreshRequest((ids) => seen.push([...ids]));
	requestMod.requestLibraryRowAssetsRefresh(['a']);
	off();
	assert.deepEqual(seen, [['a']]);
});

test('stems job ids skip rows that are not in the pane list', () => {
	const nudged = [];
	mod.applyRowAssetsRefresh(['c'], [row('a')], new WeakSet(), (ids) => nudged.push(ids));
	assert.deepEqual(nudged, []);
});

test('resync with empty ids nudges every row passed in', () => {
	const nudged = [];
	const rows = [row('a'), row('b')];
	mod.applyRowAssetsRefresh([], rows, new WeakSet(), (ids) => nudged.push([...ids]));
	assert.deepEqual(nudged, [['a', 'b']]);
});
