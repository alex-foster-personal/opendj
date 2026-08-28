/**
 * Wire-shape regression for the edit-suite / MyTag client after its conversion
 * onto the generated OpenAPI client. openapi-fetch calls fetch(request) with a
 * single Request object.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://mytags-api.example.test';
const REVISION = '"catalog-revision"';

let api;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

before(async () => {
	api = await loadTypeScriptModule('src/lib/rb/api-edit-suite.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('MyTag client reads the catalog revision and sends destructive scope acknowledgements', async () => {
	const requests = [];
	globalThis.fetch = async (request) => {
		requests.push(request);
		if (new URL(request.url).pathname.endsWith('/mytags')) {
			return jsonResponse({
				tags: [{ name: 'warmup', track_count: 3 }],
				catalog_revision: REVISION
			});
		}
		return jsonResponse({ tracks_updated: 3 });
	};

	assert.deepEqual(await api.listMyTags(), {
		tags: [{ name: 'warmup', track_count: 3 }],
		catalog_revision: REVISION
	});
	await api.renameMyTag({
		old_name: 'warmup',
		new_name: 'opening',
		expected_catalog_revision: REVISION,
		expected_track_count: 3,
		confirm_merge: true
	});
	await api.deleteMyTag({
		name: 'opening',
		expected_catalog_revision: REVISION,
		expected_track_count: 3
	});

	assert.equal(requests[1].url, `${API_BASE}/api/v1/mytags/rename`);
	assert.equal(requests[1].method, 'POST');
	assert.deepEqual(await requests[1].clone().json(), {
		old_name: 'warmup',
		new_name: 'opening',
		expected_catalog_revision: REVISION,
		expected_track_count: 3,
		confirm_merge: true
	});
	assert.equal(requests[2].url, `${API_BASE}/api/v1/mytags/delete`);
	assert.equal(requests[2].method, 'POST');
	assert.deepEqual(await requests[2].clone().json(), {
		name: 'opening',
		expected_catalog_revision: REVISION,
		expected_track_count: 3
	});
});

test('bulkEditTracks PATCHes /api/v1/bulk-edit with the exact body', async () => {
	let seen;
	let body;
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		return jsonResponse({ applied_count: 1, results: [{ stable_id: 'sid-1', etag: '"e1"' }] });
	};

	const patch = {
		stable_ids: ['sid-1'],
		expected_etags: { 'sid-1': '"e0"' },
		rating: 5,
		tags_add: ['warm']
	};
	const result = await api.bulkEditTracks(patch);

	assert.equal(seen.url, `${API_BASE}/api/v1/bulk-edit`);
	assert.equal(seen.method, 'PATCH');
	assert.equal(seen.headers.get('content-type'), 'application/json');
	assert.deepEqual(body, patch);
	assert.equal(result.applied_count, 1);
});

test('previewFindReplace POSTs /api/v1/find-replace/preview with the exact body', async () => {
	let seen;
	let body;
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		return jsonResponse({
			match_count: 1,
			results: [
				{
					stable_id: 'sid-1',
					current_value: 'old',
					new_value: 'new',
					would_change: true,
					etag: '"e1"'
				}
			]
		});
	};

	const scope = { stable_ids: ['sid-1'], find: 'old', replace: 'new', mode: 'literal' };
	const result = await api.previewFindReplace(scope);

	assert.equal(seen.url, `${API_BASE}/api/v1/find-replace/preview`);
	assert.equal(seen.method, 'POST');
	assert.deepEqual(body, scope);
	assert.equal(result.match_count, 1);
});

test('edit-suite maps detail.error (not detail.code) onto RbApiError.code', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: { error: 'SCOPE_STALE', message: 'catalog moved' } }), {
			status: 409,
			statusText: 'Conflict',
			headers: { 'content-type': 'application/json' }
		});

	const caught = await api.listMyTags().then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof Error);
	assert.equal(caught.status, 409);
	assert.equal(caught.code, 'SCOPE_STALE');
	assert.equal(caught.message, 'SCOPE_STALE: catalog moved');
});
