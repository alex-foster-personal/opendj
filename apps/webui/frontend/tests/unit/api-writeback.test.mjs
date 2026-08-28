/**
 * Wire-shape regression for the writeback client after its conversion onto the
 * generated OpenAPI client. openapi-fetch calls fetch(request) with one Request.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://writeback-api.example.test';

let writebackApi;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

const plan = {
	playlist_id: 'pl/1 a',
	vendor: 'rekordbox',
	target_mode: 'live',
	target_path: '/Volumes/USB',
	target_id: 'tgt-9',
	target_name: 'Main',
	source_revision: 'src-1',
	target_revision: 'tgt-rev-1',
	mapping_revision: 'map-1',
	ordered_match: true,
	plan_token: 'token-bound',
	added: ['a'],
	removed: ['b'],
	unresolved: [],
	is_noop: false
};

before(async () => {
	writebackApi = await loadTypeScriptModule('src/lib/rb/api-writeback.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('getWritebackPlan encodes playlist id and sends target query params', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse(plan);
	};

	const found = await writebackApi.getWritebackPlan(
		'pl/1 a',
		'rekordbox',
		'live',
		'/Volumes/USB',
		'tgt-9'
	);

	const url = new URL(seen.url);
	assert.equal(url.origin + url.pathname, `${API_BASE}/api/v1/playlists/pl%2F1%20a/writeback/plan`);
	assert.equal(url.searchParams.get('vendor'), 'rekordbox');
	assert.equal(url.searchParams.get('target_mode'), 'live');
	assert.equal(url.searchParams.get('target_path'), '/Volumes/USB');
	assert.equal(url.searchParams.get('target_id'), 'tgt-9');
	assert.equal(seen.method, 'GET');
	assert.equal(found.plan_token, 'token-bound');
});

test('applyWriteback POSTs the confirmation body with bound plan_token', async () => {
	let seen;
	let body;
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		return jsonResponse({
			playlist_id: plan.playlist_id,
			vendor: plan.vendor,
			target_id: plan.target_id,
			target_name: plan.target_name,
			applied: true,
			dry_run: false,
			added: plan.added,
			removed: plan.removed,
			backup_id: 'bak-1',
			target_revision: 'tgt-rev-2',
			error: null
		});
	};

	const result = await writebackApi.applyWriteback('pl/1 a', plan);

	assert.equal(seen.url, `${API_BASE}/api/v1/playlists/pl%2F1%20a/writeback/apply`);
	assert.equal(seen.method, 'POST');
	assert.equal(seen.headers.get('content-type'), 'application/json');
	assert.deepEqual(body, {
		vendor: 'rekordbox',
		target_mode: 'live',
		target_path: '/Volumes/USB',
		target_id: 'tgt-9',
		plan_token: 'token-bound',
		dry_run: false,
		confirmed: true
	});
	assert.equal(result.backup_id, 'bak-1');
});

test('rollbackWriteback throws before fetch when apply result has no backup', async () => {
	let fetched = false;
	globalThis.fetch = async () => {
		fetched = true;
		return jsonResponse({});
	};

	const caught = await writebackApi
		.rollbackWriteback(
			'pl-1',
			{
				playlist_id: 'pl-1',
				vendor: 'rekordbox',
				target_id: 't',
				target_name: 'n',
				applied: true,
				dry_run: false,
				added: [],
				removed: [],
				backup_id: null,
				target_revision: null,
				error: null
			},
			plan
		)
		.then(
			() => null,
			(error) => error
		);

	assert.ok(caught instanceof Error);
	assert.equal(caught.message, 'apply result has no reversible backup');
	assert.equal(fetched, false);
});

test('writeback maps detail.code/message onto RbApiError', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: { code: 'WRITEBACK_CONFLICT', message: 'target changed' } }), {
			status: 409,
			statusText: 'Conflict',
			headers: { 'content-type': 'application/json' }
		});

	const caught = await writebackApi.getWritebackCapabilities('pl-1').then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof Error);
	assert.equal(caught.status, 409);
	assert.equal(caught.code, 'WRITEBACK_CONFLICT');
	assert.equal(caught.message, 'WRITEBACK_CONFLICT: target changed');
});
