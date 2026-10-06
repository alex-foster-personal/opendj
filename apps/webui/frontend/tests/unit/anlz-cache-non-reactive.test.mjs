// B9: an ANLZ payload in the shared cache must never be deep-proxied by $state.
// Svelte's real `proxy()` is used, because under test `$state` is an identity
// function and could not tell a raw payload from a proxied one. -Claude
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { derived, get, proxy } from 'svelte/internal/client';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://anlz-cache-non-reactive.example.test';
const BINS = 38400;

let cache;
let marker;
const originalFetch = globalThis.fetch;

before(async () => {
	cache = await loadTypeScriptModule('src/lib/components/rb/wave/anlz-cache.svelte.ts', { viteApiBase: API_BASE });
	marker = await loadTypeScriptModule('src/lib/rb/non-reactive.ts');
});
after(() => {
	globalThis.fetch = originalFetch;
});

function bigPayload() {
	const band = () => Array.from({ length: BINS }, (_unused, i) => i % 256);
	return {
		stable_id: 'b9',
		points: BINS,
		waveform: { kind: 'mono', preview: { length: 0, low: [], mid: [], high: [] }, detail: { length: BINS, low: band(), mid: band(), high: band() } },
		beatgrid: { beat_count: 2, beats: [{ ms: 0, bpm: 120 }, { ms: 500, bpm: 120 }] },
		cues: [],
		phrases: [],
		local_waveform: { status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 },
		vocals: { status: 'not_analyzed' }
	};
}

test('a marked payload read back through a real Svelte proxy is the same raw object', () => {
	const payload = marker.nonReactive(bigPayload());
	const state = proxy({ entry: { status: 'ready', data: payload } });
	assert.equal(state.entry.data, payload, 'the payload must come back by reference, not as a proxy');
	assert.equal(state.entry.data.waveform.detail.low, payload.waveform.detail.low);
});

test('mutation control: an UNMARKED payload is proxied, so the identity check above can fail', () => {
	const payload = bigPayload();
	const state = proxy({ entry: { status: 'ready', data: payload } });
	assert.notEqual(state.entry.data, payload, 'a plain payload must be proxied, or the test above proves nothing');
});

test('marking leaves the payload equal to an unmarked copy under JSON and spread', () => {
	const marked = marker.nonReactive(bigPayload());
	assert.equal(JSON.stringify(marked), JSON.stringify(bigPayload()));
	assert.deepEqual({ ...marked }, { ...bigPayload() });
	assert.equal(Object.prototype.hasOwnProperty.call(marked, 'waveform'), true);
	assert.equal('waveform' in marked, true);
	assert.equal(marker.isNonReactive(marked), true);
	assert.equal(marker.isNonReactive(bigPayload()), false);
});

test('marking is idempotent and never touches arrays or class instances', () => {
	const once = marker.nonReactive({ a: 1 });
	const proto = Object.getPrototypeOf(once);
	assert.equal(Object.getPrototypeOf(marker.nonReactive(once)), proto);
	const arr = [1, 2];
	assert.equal(Object.getPrototypeOf(marker.nonReactive(arr)), Array.prototype);
	const date = new Date(0);
	assert.equal(Object.getPrototypeOf(marker.nonReactive(date)), Date.prototype);
});

test('the anlz cache publishes every ready payload marked non-reactive', async () => {
	globalThis.fetch = async () => new Response(JSON.stringify(bigPayload()), { status: 200, headers: { 'content-type': 'application/json' } });
	cache.ensureAnlz('b9-track');
	await new Promise((resolve) => setTimeout(resolve, 0));
	const entry = cache.getAnlzEntry('b9-track');
	assert.equal(entry.status, 'ready');
	// Separate bundles hold separate marker prototypes, so check the effect, not the marker.
	assert.notEqual(Object.getPrototypeOf(entry.data), Object.prototype, 'a cached payload left plain grows one signal per waveform bin');
	const state = proxy({ cache: { 'b9-track': entry } });
	assert.equal(state.cache['b9-track'].data, entry.data);
	assert.equal(state.cache['b9-track'].data.waveform.detail.low.length, BINS);
});

test('a reader of a marked payload updates when the cache entry is REPLACED (re-analysis, refresh, revalidate)', () => {
	const state = proxy({ cache: { t: { status: 'ready', data: marker.nonReactive({ points: 1 }) } } });
	const points = derived(() => state.cache.t.data.points);
	assert.equal(get(points), 1);
	state.cache.t = { status: 'ready', data: marker.nonReactive({ points: 2 }) };
	assert.equal(get(points), 2, 'replacing the entry is how every refresh path publishes, and it must still notify');
	// The documented trade: an in-place edit is NOT observed. No such edit exists in src/ (B9 audit).
	state.cache.t.data.points = 3;
	assert.equal(get(points), 2);
});
