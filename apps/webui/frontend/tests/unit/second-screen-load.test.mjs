import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let audioContextCount = 0;
let savedAudioContext;
let savedWebkitAudioContext;

before(async () => {
	savedAudioContext = globalThis.AudioContext;
	savedWebkitAudioContext = globalThis.webkitAudioContext;
	audioContextCount = 0;
	class CountingAudioContext {
		constructor() {
			audioContextCount += 1;
		}
	}
	globalThis.AudioContext = CountingAudioContext;
	globalThis.webkitAudioContext = CountingAudioContext;
	mod = await loadTypeScriptModule('src/lib/library/second-screen-load.ts');
});

after(() => {
	if (savedAudioContext === undefined) {
		delete globalThis.AudioContext;
	} else {
		globalThis.AudioContext = savedAudioContext;
	}
	if (savedWebkitAudioContext === undefined) {
		delete globalThis.webkitAudioContext;
	} else {
		globalThis.webkitAudioContext = savedWebkitAudioContext;
	}
});

test('load order is the dispatcher single-load shape', () => {
	assert.deepEqual(mod.buildLoadOrder(2, 'abc'), {
		single: { type: 'load', deck: 2, stable_id: 'abc' }
	});
	assert.throws(() => mod.buildLoadOrder(1, '  '), TypeError);
});

test('next free deck is the lowest empty deck', () => {
	assert.equal(
		mod.nextFreeDeck({ 1: { stable_id: 'a' }, 2: { stable_id: 'b' }, 3: { stable_id: null } }),
		3
	);
	assert.equal(mod.nextFreeDeck({}), 1);
});

test('no free deck is null, never a guess', () => {
	const full = {
		1: { stable_id: 'a' },
		2: { stable_id: 'b' },
		3: { stable_id: 'c' },
		4: { stable_id: 'd' }
	};
	assert.equal(mod.nextFreeDeck(full), null);
});

test('409 says the main window is not open', () => {
	const out = mod.describeLoadResult(1, 409, { client_open: false });
	assert.equal(out.ok, false);
	assert.match(out.message, /main Open DJ window is not open/);
});

test('only all-succeeded steps read as loaded', () => {
	assert.equal(mod.describeLoadResult(3, 200, { steps: [{ status: 'succeeded' }] }).ok, true);
	const failed = mod.describeLoadResult(3, 200, {
		steps: [{ status: 'failed', error: 'deck 3 is playing' }]
	});
	assert.equal(failed.ok, false);
	assert.match(failed.message, /deck 3 is playing/);
	assert.equal(mod.describeLoadResult(3, 200, { steps: [{ status: 'skipped' }] }).ok, false);
	assert.equal(mod.describeLoadResult(3, 200, { steps: [] }).ok, false);
	assert.equal(mod.describeLoadResult(3, 503, null).ok, false);
});

test('remaining label is m:ss or null', () => {
	assert.equal(mod.remainingLabel({ position: { ms: 1000 }, duration_ms: 62000 }), '-1:01');
	assert.equal(mod.remainingLabel({ position: null, duration_ms: 1 }), null);
});

test('load helpers create no AudioContext', () => {
	mod.buildLoadOrder(2, 'abc');
	mod.nextFreeDeck({ 1: { stable_id: 'a' }, 2: { stable_id: null } });
	mod.describeLoadResult(1, 409, { client_open: false });
	mod.remainingLabel({ position: { ms: 0 }, duration_ms: 60_000 });
	assert.equal(audioContextCount, 0);
});
