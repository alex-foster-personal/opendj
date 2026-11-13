// requirement: PERF-DIAG-9990
// [if] an engine response carries Server-Timing app;dur=<ms> [then] the deck-load row gets that member's service_ms, [else stop].
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const timing = await loadTypeScriptModule('src/lib/rb/server-timing.ts');

/** A fake fetch response whose only header is the given Server-Timing value (or none). */
function fakeResponse(serverTiming) {
	const headers = new Headers(serverTiming === undefined ? {} : { 'Server-Timing': serverTiming });
	return { headers };
}

test('parseServerTiming reads the app metric dur among several metrics', () => {
	assert.equal(timing.parseServerTiming('app;dur=12.5, db;dur=3'), 12.5);
	assert.equal(timing.parseServerTiming('db;dur=3, app;dur=7'), 7);
	assert.equal(timing.parseServerTiming('app;desc="engine";dur=4.0'), 4);
});

test('parseServerTiming answers null, never 0, for a missing or malformed value', () => {
	for (const header of [
		null,
		undefined,
		'',
		'db;dur=3',
		'app',
		'app;dur=',
		'app;dur=abc',
		'app;dur=-1'
	]) {
		assert.equal(timing.parseServerTiming(header), null, String(header));
	}
});

test('deckLoadServiceMs records each member whose response carried the header', () => {
	const serviceMs = timing.deckLoadServiceMs({
		getTrack: fakeResponse('app;dur=4.2'),
		fetchAnlz: fakeResponse('app;dur=120'),
		fetchHotCues: fakeResponse('app;dur=9.5'),
		fetchAudio: fakeResponse('app;dur=380.1')
	});
	assert.deepEqual(serviceMs, {
		getTrack: 4.2,
		fetchAnlz: 120,
		fetchHotCues: 9.5,
		fetchAudio: 380.1
	});
});

test('deckLoadServiceMs leaves a member absent when its response has no header', () => {
	const serviceMs = timing.deckLoadServiceMs({
		getTrack: fakeResponse('app;dur=4.2'),
		fetchAnlz: fakeResponse(undefined),
		fetchHotCues: fakeResponse('app;dur=oops')
	});
	assert.deepEqual(serviceMs, { getTrack: 4.2 });
	assert.equal('fetchAnlz' in serviceMs, false);
	assert.equal('fetchAudio' in serviceMs, false);
});
