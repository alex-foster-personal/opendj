import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('LRU drops oldest ready entry when over cap', async () => {
	const { evictAnlzLru } = await loadTypeScriptModule(
		'src/lib/components/rb/wave/anlz-cache-caps.ts'
	);
	const cache = new Map(Object.entries({
		a: { status: 'ready', touched: 1 },
		b: { status: 'ready', touched: 2 },
		c: { status: 'loading' }
	}));
	evictAnlzLru(cache, {
		entryCap: 1,
		byteCap: 10 * 1024 * 1024,
		estimateBytes: () => 1024,
		isReady: (entry) => entry.status === 'ready'
	});
	assert.equal(cache.has('a'), false);
	assert.equal(cache.has('b'), true);
	assert.equal(cache.has('c'), true);
});

test('retouch bumps ready entry touched seq', async () => {
	const { nextAnlzTouch, retouchAnlzReadyEntry } = await loadTypeScriptModule(
		'src/lib/components/rb/wave/anlz-cache-caps.ts'
	);
	const cache = new Map([['a', { status: 'ready', touched: nextAnlzTouch() }]]);
	const before = cache.get('a').touched;
	retouchAnlzReadyEntry(cache, 'a');
	assert.ok(cache.get('a').touched > before);
});

/** Five ready entries, oldest first; touched seqs come from the real counter. */
function readyCache(mod, ids) {
	const cache = new Map();
	for (const id of ids) cache.set(id, { status: 'ready', touched: mod.nextAnlzTouch() });
	return cache;
}

test('PERFMODE-15: a held entry cap evicts down to it at once, keeping the newest', async () => {
	const mod = await loadTypeScriptModule('src/lib/components/rb/wave/anlz-cache-caps.ts');
	const cache = readyCache(mod, ['a', 'b', 'c', 'd', 'e']);
	mod.bindAnlzCapCache(cache);
	assert.ok(mod.effectiveAnlzEntryCap() >= 5, 'control: with no hold, the tier cap keeps all five');
	const release = mod.holdAnlzEntryCap(1);
	assert.deepEqual([...cache.keys()], ['e']);
	assert.equal(mod.effectiveAnlzEntryCap(), 1);
	release();
});

test('PERFMODE-15: releasing the hold restores the tier cap for later inserts', async () => {
	const mod = await loadTypeScriptModule('src/lib/components/rb/wave/anlz-cache-caps.ts');
	const cache = readyCache(mod, ['a']);
	mod.bindAnlzCapCache(cache);
	const tierCap = mod.effectiveAnlzEntryCap();
	mod.holdAnlzEntryCap(1)();
	for (const id of ['b', 'c', 'd']) cache.set(id, { status: 'ready', touched: mod.nextAnlzTouch() });
	mod.applyAnlzCaps();
	// Overshoot control: a hold that outlived its release would leave one entry.
	assert.deepEqual([...cache.keys()].sort(), ['a', 'b', 'c', 'd']);
	assert.equal(mod.effectiveAnlzEntryCap(), tierCap);
});

test('PERFMODE-15: the smallest live hold wins, and a double release fails loud', async () => {
	const mod = await loadTypeScriptModule('src/lib/components/rb/wave/anlz-cache-caps.ts');
	mod.bindAnlzCapCache(new Map());
	const releaseOne = mod.holdAnlzEntryCap(1);
	const releaseThree = mod.holdAnlzEntryCap(3);
	assert.equal(mod.effectiveAnlzEntryCap(), 1);
	releaseOne();
	assert.equal(mod.effectiveAnlzEntryCap(), 3);
	assert.throws(() => releaseOne(), /released twice/);
	releaseThree();
	assert.throws(() => mod.holdAnlzEntryCap(0), RangeError);
});
