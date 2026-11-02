/**
 * CUEOUT-6060: the snap chunk's grid cache is a count-capped LRU, so warm-on-hover
 * over a long scroll session cannot hold every dwelt row's grid until the
 * preview's idle release (src/lib/player/preview-beat-snap-start.ts).
 *
 * Tested on the pure BoundedLru with plain values. The fetch path is not faked
 * here: the production fetch and its generation keying run in the preview e2e
 * spec against the real analysis fixtures.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let BoundedLru;
let PREVIEW_GRID_CACHE_MAX;

before(async () => {
	({ BoundedLru, PREVIEW_GRID_CACHE_MAX } = await loadTypeScriptModule(
		'src/lib/player/preview-beat-snap-start.ts'
	));
});

test('the cache never holds more than its cap', () => {
	const lru = new BoundedLru(PREVIEW_GRID_CACHE_MAX);
	for (let i = 0; i < PREVIEW_GRID_CACHE_MAX * 3; i += 1) lru.put(`t${i}`, i);
	assert.equal(lru.size, PREVIEW_GRID_CACHE_MAX);
});

test('the least recently used entry is the one dropped, and a read refreshes recency', () => {
	const lru = new BoundedLru(PREVIEW_GRID_CACHE_MAX);
	for (let i = 0; i < PREVIEW_GRID_CACHE_MAX; i += 1) lru.put(`t${i}`, i);
	assert.equal(lru.get('t0'), 0, 't0 is held before the read that refreshes it');
	lru.put('new', -1); // evicts t1, the oldest now that t0 was read
	assert.equal(lru.get('t0'), 0, 't0 was refreshed by its read, so it must still be held');
	assert.equal(lru.get('t1'), undefined, 't1 was the oldest, so it must have been dropped');
	assert.equal(lru.get('new'), -1);
});

test('control: a key within the cap is held and read back unchanged', () => {
	const lru = new BoundedLru(2);
	lru.put('a', 1);
	lru.put('b', 2);
	assert.equal(lru.get('a'), 1);
	assert.equal(lru.get('b'), 2);
	assert.equal(lru.size, 2);
});

test('a cap that is not a positive integer is refused, not silently unbounded', () => {
	assert.throws(() => new BoundedLru(0), RangeError);
	assert.throws(() => new BoundedLru(1.5), RangeError);
});
