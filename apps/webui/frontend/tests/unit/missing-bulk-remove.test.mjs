import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Missing Tracks bulk remove client (LIBM-78b, issue #12316).
 *
 * [if] the selection is not sent as one POST of stable_ids [then] the server guard never sees the whole batch - broken.
 * [if] a 409 not_missing loses its code or message [then] the toast cannot say why nothing was removed - broken.
 * [if] an empty selection reaches the server [then] the button issues a pointless write - broken.
 */

const API_BASE = 'https://missing-remove.example.test';

let reconcile;
let originalFetch;

before(async () => {
	reconcile = await loadTypeScriptModule('src/lib/reconcile-api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

function _json(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

test('the selection goes out as one POST of stable_ids and the result comes back', async () => {
	/** [if] two ids are removed [then] one POST to /reconcile/broken:remove carries both and the body is returned, [else stop]. */
	const seen = [];
	globalThis.fetch = async (request) => {
		seen.push({ url: request.url, method: request.method, body: await request.clone().json() });
		return _json({ removed: ['a', 'b'], playlist_ids: ['pl-1'] });
	};
	const result = await reconcile.removeMissingTracks(['a', 'b']);
	assert.deepEqual(seen, [
		{
			url: `${API_BASE}/api/v1/reconcile/broken:remove`,
			method: 'POST',
			body: { stable_ids: ['a', 'b'] }
		}
	]);
	assert.deepEqual(result, { removed: ['a', 'b'], playlist_ids: ['pl-1'] });
});

test('a not_missing refusal surfaces its code and message', async () => {
	/** [if] the server answers 409 not_missing [then] the call rejects with that code and the server message, [else stop]. */
	globalThis.fetch = async () =>
		_json(
			{
				detail: {
					code: 'not_missing',
					message: '1 selected track(s) are no longer missing',
					stable_ids: ['b']
				}
			},
			409
		);
	await assert.rejects(reconcile.removeMissingTracks(['a', 'b']), (error) => {
		assert.equal(error.status, 409);
		assert.equal(error.code, 'not_missing');
		assert.match(error.message, /no longer missing/);
		return true;
	});
});

test('an empty selection never reaches the server', async () => {
	/** [if] no row is selected [then] the call throws before any fetch, [else stop]. */
	let called = false;
	globalThis.fetch = async () => {
		called = true;
		return _json({});
	};
	await assert.rejects(reconcile.removeMissingTracks([]), RangeError);
	assert.equal(called, false);
});
