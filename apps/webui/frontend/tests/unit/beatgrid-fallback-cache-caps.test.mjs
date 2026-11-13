import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('70 gridless tracks leave 64 ready entries, oldest evicted first, loading kept', async () => {
	/** [if] 70 ready tracks plus one loading entry resolve [then] 64 newest ready stay and loading survives, [else stop]. */
	const caps = await loadTypeScriptModule(
		'src/lib/components/rb/wave/beatgrid-fallback-cache-caps.ts'
	);
	assert.equal(caps.beatgridFallbackEntryCap(), 64);
	let cache = { inflight: { status: 'loading' } };
	for (let i = 0; i < 70; i++) {
		cache = { ...cache, [`t${i}`]: { status: 'ready', touched: caps.nextBeatgridFallbackTouch() } };
		cache = caps.evictBeatgridFallbackLru(cache, caps.beatgridFallbackEntryCap());
	}
	const ready = Object.keys(cache).filter((id) => cache[id].status === 'ready');
	assert.equal(ready.length, 64);
	for (let i = 0; i < 6; i++) assert.equal(cache[`t${i}`], undefined);
	assert.equal(cache.t6?.status, 'ready');
	assert.equal(cache.inflight.status, 'loading');
	cache = caps.retouchBeatgridFallbackEntry(cache, 't6');
	cache = caps.evictBeatgridFallbackLru(
		{ ...cache, t70: { status: 'ready', touched: caps.nextBeatgridFallbackTouch() } },
		64
	);
	assert.equal(cache.t6?.status, 'ready');
	assert.equal(cache.t7, undefined);
});
