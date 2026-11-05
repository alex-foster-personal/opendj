import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/** PERF-LEAK-04: estimateDeckPcmBytes must charge every PCM copy the engine retains. */
let estimateDeckPcmBytes;

before(async () => {
	({ estimateDeckPcmBytes } = await loadTypeScriptModule('src/lib/rb/deck-audio-snapshot.ts'));
});

const mixBuffer = { length: 1000, numberOfChannels: 2 };
const MIX = 8000;

function deck(overrides) {
	return {
		audioBuffer: mixBuffer,
		stemsReady: false,
		mixWorkletCopy: false,
		pendingStemUpgrade: false,
		...overrides
	};
}

test('empty iterable returns zero', () => {
	assert.equal(estimateDeckPcmBytes([]), 0);
});

test('unloaded deck contributes nothing', () => {
	assert.equal(estimateDeckPcmBytes([deck({ audioBuffer: null, mixWorkletCopy: true })]), 0);
});

test('mix only charges JS plus worklet copy', () => {
	assert.equal(
		estimateDeckPcmBytes([deck({ mixWorkletCopy: true })]),
		MIX * 2,
		'mix-only is 2x mixBytes'
	);
});

test('mix with pending stems charges six copies', () => {
	assert.equal(
		estimateDeckPcmBytes([deck({ mixWorkletCopy: true, pendingStemUpgrade: true })]),
		MIX * 6
	);
});

test('AUDFID-02 hold charges six copies', () => {
	assert.equal(
		estimateDeckPcmBytes([deck({ mixWorkletCopy: true, stemsReady: true })]),
		MIX * 6
	);
});

test('stems engaged without mix worklet charges five copies', () => {
	assert.equal(
		estimateDeckPcmBytes([deck({ stemsReady: true, mixWorkletCopy: false })]),
		MIX * 5
	);
});

test('both stem flags add four stem parts once, never eight', () => {
	assert.equal(
		estimateDeckPcmBytes([
			deck({ mixWorkletCopy: true, stemsReady: true, pendingStemUpgrade: true })
		]),
		MIX * 6,
		'ready and pending together must not double the stem term'
	);
	assert.equal(
		estimateDeckPcmBytes([deck({ stemsReady: true, pendingStemUpgrade: true, mixWorkletCopy: false })]),
		MIX * 5
	);
});
