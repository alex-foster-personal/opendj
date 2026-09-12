import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('LRU drops oldest ready entry when over cap', async () => {
	const { evictAnlzLru } = await loadTypeScriptModule(
		'src/lib/components/rb/wave/anlz-cache-caps.ts'
	);
	const cache = {
		a: { status: 'ready', touched: 1 },
		b: { status: 'ready', touched: 2 },
		c: { status: 'loading' }
	};
	evictAnlzLru(cache, {
		entryCap: 1,
		byteCap: 10 * 1024 * 1024,
		estimateBytes: () => 1024,
		isReady: (entry) => entry.status === 'ready'
	});
	assert.equal('a' in cache, false);
	assert.equal('b' in cache, true);
	assert.equal('c' in cache, true);
});

test('retouch bumps ready entry touched seq', async () => {
	const { nextAnlzTouch, retouchAnlzReadyEntry } = await loadTypeScriptModule(
		'src/lib/components/rb/wave/anlz-cache-caps.ts'
	);
	const cache = { a: { status: 'ready', touched: nextAnlzTouch() } };
	const before = cache.a.touched;
	retouchAnlzReadyEntry(cache, 'a');
	assert.ok(cache.a.touched > before);
});
