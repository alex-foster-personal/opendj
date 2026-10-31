// requirement: LIBWS-06
// [if] Back/Forward or a goto leaves Library for Gig while a Gig teardown is in flight [then] it is cancelled and replayed once the teardown settles
// [if] no teardown is in flight [then] the navigation goes ahead untouched
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let gate;

before(async () => {
	gate = await loadTypeScriptModule('src/lib/library/library-exit-gate.ts');
});

function harness(pending) {
	const log = [];
	let release;
	const deps = {
		pending: () => pending,
		settled: () =>
			new Promise((resolve) => {
				release = resolve;
			}),
		go: (delta) => log.push(['go', delta]),
		goto: async (url) => {
			log.push(['goto', url.pathname]);
		}
	};
	const nav = (type, extra = {}) => ({
		type,
		to: { url: new URL('http://x/performance') },
		cancel: () => log.push(['cancel']),
		...extra
	});
	return { log, deps, nav, settle: () => release() };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

test('Back during a Gig teardown is cancelled, then replayed by the same step once it settles', async () => {
	const h = harness(true);
	assert.equal(gate.holdLibraryExitForGigRelease(h.nav('popstate', { delta: -1 }), h.deps), true);
	await flush();
	assert.deepEqual(h.log, [['cancel']], 'replayed before the teardown settled');
	h.settle();
	await flush();
	assert.deepEqual(h.log, [['cancel'], ['go', -1]]);
});

test('a goto or link during a teardown is replayed to the same URL', async () => {
	for (const type of ['goto', 'link']) {
		const h = harness(true);
		assert.equal(gate.holdLibraryExitForGigRelease(h.nav(type), h.deps), true);
		h.settle();
		await flush();
		assert.deepEqual(h.log, [['cancel'], ['goto', '/performance']], type);
	}
});

test('with no teardown in flight, nothing is cancelled', () => {
	const h = harness(false);
	for (const type of ['popstate', 'goto', 'link']) {
		assert.equal(gate.holdLibraryExitForGigRelease(h.nav(type, { delta: 1 }), h.deps), false);
	}
	assert.deepEqual(h.log, []);
});

test('kinds that cannot be replayed (form, leave) and a null target are left alone', () => {
	const h = harness(true);
	assert.equal(gate.holdLibraryExitForGigRelease(h.nav('form'), h.deps), false);
	assert.equal(gate.holdLibraryExitForGigRelease(h.nav('leave'), h.deps), false);
	assert.equal(
		gate.holdLibraryExitForGigRelease({ ...h.nav('popstate', { delta: -1 }), to: null }, h.deps),
		false
	);
	assert.deepEqual(h.log, []);
});
