/**
 * Who asks for a cached coverage measurement and who asks for a fresh one
 * (HEALTH-12).
 *
 * [if] getIngestCoverage({ cached: true }) is called [then ⛔] the request
 *   must carry cached=true, or the lights wait on a whole-library re-measure.
 * [if] getIngestCoverage() is called with no options [then ⛔] the request
 *   must NOT carry cached, or a caller that acts on the count gets an old one.
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const ingest = await loadTypeScriptModule('src/lib/rb/api-ingest.ts');

let realFetch;
let urls;

beforeEach(() => {
	realFetch = globalThis.fetch;
	urls = [];
	globalThis.fetch = async (url) => {
		urls.push(String(url));
		return new Response(JSON.stringify({ on_disk: 0 }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
});

afterEach(() => {
	globalThis.fetch = realFetch;
});

// REQ: HEALTH-12
test('a cached read asks the engine for the last measurement', async () => {
	await ingest.getIngestCoverage({ cached: true });
	assert.equal(urls.length, 1);
	assert.match(urls[0], /\/api\/v1\/ingest\/coverage\?cached=true$/);
});

// REQ: HEALTH-12
test('the default read stays a fresh measurement', async () => {
	await ingest.getIngestCoverage();
	assert.equal(urls.length, 1);
	assert.match(urls[0], /\/api\/v1\/ingest\/coverage$/);
});
