// B9: the ANLZ cache keeps payloads RAW (SvelteMap, per-key reactive) and a
// replaced entry still reaches its readers. Runs the REAL Svelte runtime via
// load-rune-module.mjs: under load-typescript.mjs `$state` is an identity
// function and could not tell a raw payload from a deep proxy. -Claude
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const BINS = 38400;
const originalFetch = globalThis.fetch;
after(() => {
	globalThis.fetch = originalFetch;
});

// The control: what the cache used to be, a deep $state record.
const ENTRY = [
	"export { ensureAnlz, getAnlzEntry, refreshAnlzCacheEntry, invalidateAnlzCacheEntry } from '$lib/components/rb/wave/anlz-cache.svelte';",
	"import { flushSync } from 'svelte';",
	"import { getAnlzEntry } from '$lib/components/rb/wave/anlz-cache.svelte';",
	'const deepRecord = $state<Record<string, unknown>>({});',
	'export function throughDeepState(key: string, value: unknown): unknown { deepRecord[key] = value; return deepRecord[key]; }',
	'export function watchPoints(sid: string): { seen: unknown[]; stop: () => void } {',
	'	const seen: unknown[] = [];',
	'	const stop = $effect.root(() => { $effect(() => { const e = getAnlzEntry(sid); seen.push(e?.status === "ready" ? e.data.points : (e?.status ?? null)); }); });',
	'	flushSync();',
	'	return { seen, stop };',
	'}',
	'export { flushSync };'
].join('\n');

function payload(points) {
	const band = () => Array.from({ length: BINS }, (_unused, i) => i % 256);
	return {
		stable_id: 'b9',
		points,
		waveform: { kind: 'mono', preview: { length: 0, low: [], mid: [], high: [] }, detail: { length: BINS, low: band(), mid: band(), high: band() } },
		beatgrid: { beat_count: 2, beats: [{ ms: 0, bpm: 120 }, { ms: 500, bpm: 120 }] },
		cues: [],
		phrases: [],
		local_waveform: { status: 'decoded', reason: null, preview_b64: 'AAAA', preview_max: 200 },
		vocals: { status: 'not_analyzed' }
	};
}

/** A Svelte proxy cannot be structured-cloned; a raw object can. */
const isProxied = (value) => {
	try {
		structuredClone(value);
		return false;
	} catch (error) {
		if (error instanceof DOMException && error.name === 'DataCloneError') return true;
		throw error;
	}
};

test('a cached payload is stored raw: no signal per waveform bin', async () => {
	const live = await loadRuneModule(ENTRY);
	globalThis.fetch = async () => new Response(JSON.stringify(payload(BINS)), { status: 200, headers: { 'content-type': 'application/json' } });
	live.ensureAnlz('raw-track');
	await new Promise((resolve) => setTimeout(resolve, 0));
	const entry = live.getAnlzEntry('raw-track');
	assert.equal(entry.status, 'ready');
	assert.equal(isProxied(entry.data), false, 'a proxied payload grows ~100k signals per track (B9)');
	assert.equal(isProxied(entry.data.waveform.detail.low), false);
});

test('mutation control: the same payload through a deep $state IS proxied', async () => {
	const live = await loadRuneModule(ENTRY);
	assert.equal(isProxied(live.throughDeepState('k', payload(BINS))), true, 'else the check above proves nothing');
});

test('re-analysing a track refreshes its waveform: a replaced entry reaches a live reader', async () => {
	const live = await loadRuneModule(ENTRY);
	const watch = live.watchPoints('reanalysed');
	try {
		live.refreshAnlzCacheEntry('reanalysed', payload(100));
		live.flushSync();
		live.refreshAnlzCacheEntry('reanalysed', payload(200));
		live.flushSync();
		live.invalidateAnlzCacheEntry('reanalysed');
		live.flushSync();
		assert.deepEqual(watch.seen, [null, 100, 200, null]);
	} finally {
		watch.stop();
	}
});
