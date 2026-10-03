import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// column-view (browser-surface unit, Miller-column lane). Regression lines:
// - if artistBuckets/albumBuckets don't count every row exactly once then broken
// - if the null (missing-value) bucket gets folded into 'All' then a real absence is hidden
// - if the null bucket doesn't sink last then it reads as a real alphabetical value
// - if albumBuckets doesn't narrow by the selected artist then the second column is wrong
// - if filterByColumn's undefined ('All') vs null ('missing') selectors are conflated then broken

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/browser/column-buckets.ts');
});

function _row(artist, album) {
	return { artist, album };
}

const ROWS = [
	_row('Glass Meridian', 'Quiet Lab Sessions'),
	_row('Glass Meridian', 'Quiet Lab Sessions'),
	_row('Glass Meridian', 'Aerial Notes'),
	_row('Tide Cartographers', 'Maps for Small Weather'),
	_row('Tide Cartographers', null),
	_row(null, null)
];

test('artistBuckets: counts every row exactly once, real values alphabetical, null sinks last', () => {
	const buckets = mod.artistBuckets(ROWS);
	assert.deepEqual(buckets, [
		{ value: 'Glass Meridian', count: 3 },
		{ value: 'Tide Cartographers', count: 2 },
		{ value: null, count: 1 }
	]);
});

test('albumBuckets: undefined artist selector (All) covers every row', () => {
	const buckets = mod.albumBuckets(ROWS, undefined);
	const total = buckets.reduce((sum, b) => sum + b.count, 0);
	assert.equal(total, ROWS.length);
});

test('albumBuckets: narrows by the selected artist', () => {
	const buckets = mod.albumBuckets(ROWS, 'Glass Meridian');
	assert.deepEqual(buckets, [
		{ value: 'Aerial Notes', count: 1 },
		{ value: 'Quiet Lab Sessions', count: 2 }
	]);
});

test('albumBuckets: the null artist bucket is a real, selectable filter (not folded into All)', () => {
	const buckets = mod.albumBuckets(ROWS, null);
	assert.deepEqual(buckets, [{ value: null, count: 1 }]);
});

test('filterByColumn: undefined/undefined (All/All) returns every row', () => {
	assert.equal(mod.filterByColumn(ROWS, undefined, undefined).length, ROWS.length);
});

test('filterByColumn: artist + album both narrow together', () => {
	const filtered = mod.filterByColumn(ROWS, 'Glass Meridian', 'Quiet Lab Sessions');
	assert.equal(filtered.length, 2);
	assert.ok(filtered.every((r) => r.artist === 'Glass Meridian' && r.album === 'Quiet Lab Sessions'));
});

test('filterByColumn: null artist selector isolates rows with a genuinely missing artist', () => {
	const filtered = mod.filterByColumn(ROWS, null, undefined);
	assert.deepEqual(filtered, [_row(null, null)]);
});

test('artistBuckets: empty input is an empty bucket list, not an error', () => {
	assert.deepEqual(mod.artistBuckets([]), []);
});
