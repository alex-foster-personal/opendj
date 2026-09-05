/**
 * putIngestConfig must push the freshly written config out to any
 * subscriber (AnalysisDotsPopover.svelte's shared 15s-TTL cache is the
 * real caller) the instant a write succeeds, rather than leaving that
 * subscriber to serve a stale read for up to its own TTL.
 *
 * [if] putIngestConfig succeeds [then ⛔] a listener registered via
 *   onIngestConfigWrite must receive the exact fresh config, synchronously
 *   with the resolved promise, not on some later tick.
 * [if] putIngestConfig fails (non-2xx) [then ⛔] no listener fires - a
 *   failed write must not be reported as if it changed anything.
 * [if] getIngestConfig is called [then ⛔] it never touches listeners -
 *   only a write is a change worth propagating.
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const ingest = await loadTypeScriptModule('src/lib/rb/api-ingest.ts');

let realFetch;
let respond;

beforeEach(() => {
	realFetch = globalThis.fetch;
	respond = () =>
		new Response(JSON.stringify({ steps: [], path: '/music' }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	globalThis.fetch = async () => respond();
});

afterEach(() => {
	globalThis.fetch = realFetch;
});

test('putIngestConfig fires onIngestConfigWrite listeners with the fresh config', async () => {
	const seen = [];
	ingest.onIngestConfigWrite((cfg) => seen.push(cfg));

	const returned = await ingest.putIngestConfig({ analysis: true });

	assert.equal(seen.length, 1, 'listener must fire exactly once per successful write');
	assert.deepEqual(seen[0], returned, 'listener must receive the same fresh config the caller got back');
});

test('putIngestConfig does not fire listeners on a failed write', async () => {
	respond = () => new Response('nope', { status: 500 });
	const seen = [];
	ingest.onIngestConfigWrite((cfg) => seen.push(cfg));

	await assert.rejects(() => ingest.putIngestConfig({ analysis: true }));

	assert.equal(seen.length, 0, 'a failed write must not be reported as a change');
});

test('getIngestConfig never fires onIngestConfigWrite listeners', async () => {
	const seen = [];
	ingest.onIngestConfigWrite((cfg) => seen.push(cfg));

	await ingest.getIngestConfig();

	assert.equal(seen.length, 0, 'a plain read is not a write and must not invalidate anything');
});
