/**
 * Wire-shape regression for the settings AI client after its conversion onto
 * the generated OpenAPI client.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://ai-client.example.test';

let aiClient;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

before(async () => {
	aiClient = await loadTypeScriptModule('src/lib/settings/ai-client.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('aiSearchSettings posts query and catalog ids to the search endpoint', async () => {
	let request;
	let body;
	globalThis.fetch = async (input) => {
		request = input;
		body = await input.clone().json();
		return jsonResponse({ ids: ['tempo', 'key'], model: 'test-model' });
	};

	const result = await aiClient.aiSearchSettings('tempo', ['tempo', 'key', 'quantise']);

	assert.equal(request.url, `${API_BASE}/api/v1/settings/ai-search`);
	assert.equal(request.method, 'POST');
	assert.equal(request.headers.get('content-type'), 'application/json');
	assert.deepEqual(body, { query: 'tempo', catalog_ids: ['tempo', 'key', 'quantise'] });
	assert.deepEqual(result, { ids: ['tempo', 'key'], model: 'test-model' });
});

test('aiApplySetting posts the instruction and returns the proposal', async () => {
	let request;
	let body;
	globalThis.fetch = async (input) => {
		request = input;
		body = await input.clone().json();
		return jsonResponse({
			ok: true,
			proposal: { key: 'theme', value: 'dark', rationale: 'night' },
			error: null,
			model: 'test-model'
		});
	};

	const result = await aiClient.aiApplySetting('use dark theme');

	assert.equal(request.url, `${API_BASE}/api/v1/settings/ai-apply`);
	assert.equal(request.method, 'POST');
	assert.deepEqual(body, { instruction: 'use dark theme' });
	assert.equal(result.ok, true);
	assert.equal(result.proposal.key, 'theme');
});

test('a non-2xx detail envelope maps to Error with path and status', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ detail: { code: 'AI_OFF', message: 'disabled' } }, { status: 503, statusText: 'Unavailable' });

	const caught = await aiClient.aiSearchSettings('x', []).then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof Error);
	assert.match(caught.message, /\/api\/v1\/settings\/ai-search/);
	assert.match(caught.message, /503/);
	assert.match(caught.message, /AI_OFF|disabled/);
});
