/**
 * GRID-10767: the analysis popover's Dismiss/Restore button persists through
 * the same route the CLI (`opendj track grid-flags`) uses.
 *
 * Regression lines:
 * - if Dismiss does not PUT /api/v1/beatgrid-flags/<id>/dismissed {"dismissed": true} then broken
 * - if Restore does not send {"dismissed": false} on the same route then broken
 * - if a 4xx error carries raw JSON instead of the route's detail then the toast is unreadable
 * - if a 409 resolves instead of rejecting then the row flips on a refused write
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'http://engine.test';
let client;
let originalFetch;

function stubFetch(response) {
	const calls = [];
	globalThis.fetch = async (url, init) => {
		calls.push({ url: String(url), init });
		return response();
	};
	return calls;
}

before(async () => {
	client = await loadTypeScriptModule('src/lib/rb/api-grid-flags.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

for (const dismissed of [true, false]) {
	test(`[if] the button sends dismissed=${dismissed} [then] PUT /api/v1/beatgrid-flags/<id>/dismissed`, async () => {
		const answer = { stable_id: 'sid 1', dismissed, etag: 'e2' };
		const calls = stubFetch(() => new Response(JSON.stringify(answer), { status: 200 }));
		const result = await client.setGridFlagDismissed('sid 1', dismissed);
		assert.equal(calls.length, 1);
		assert.equal(calls[0].url, `${API_BASE}/api/v1/beatgrid-flags/sid%201/dismissed`);
		assert.equal(calls[0].init.method, 'PUT');
		assert.equal(calls[0].init.headers['Content-Type'], 'application/json');
		assert.deepEqual(JSON.parse(calls[0].init.body), { dismissed });
		assert.deepEqual(result, answer);
	});
}

test('[if] the route answers 404 with a detail [then] the error carries that detail, not raw JSON', async () => {
	stubFetch(
		() => new Response(JSON.stringify({ detail: 'Track not found: sid' }), { status: 404 })
	);
	await assert.rejects(client.setGridFlagDismissed('sid', true), (error) => {
		assert.match(error.message, /HTTP 404 Track not found: sid/);
		assert.ok(!error.message.includes('{"detail"'));
		return true;
	});
});

test('[if] the route answers 409 [then] it rejects, so the owner never writes the row', async () => {
	stubFetch(() => new Response(JSON.stringify({ detail: 'etag conflict' }), { status: 409 }));
	await assert.rejects(client.setGridFlagDismissed('sid', false), /HTTP 409 etag conflict/);
});
