/**
 * Contract tests for the one generated-OpenAPI client (src/lib/api/client.ts).
 *
 * These pin the plumbing every converted module inherits: the base URL, the
 * always-throw middleware, and the {"detail": {code, message}} decoding.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://client.example.test';

let clientModule;
let originalFetch;

before(async () => {
	clientModule = await loadTypeScriptModule('src/lib/api/client.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('a 2xx returns parsed data and the raw response for header reads', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ status: 'ok' }), {
			status: 200,
			headers: { 'content-type': 'application/json', etag: '"rev-1"' }
		});

	const { data, response } = await clientModule.api.GET('/api/v1/health');

	assert.equal(data.status, 'ok');
	assert.equal(response.headers.get('etag'), '"rev-1"');
});

test('the request URL is the configured base plus the schema path', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return new Response(JSON.stringify({ status: 'ok' }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};

	await clientModule.api.GET('/api/v1/health');

	assert.equal(seen.url, `${API_BASE}/api/v1/health`);
	assert.equal(clientModule.API_BASE, API_BASE);
});

test('a non-2xx throws ApiError carrying the detail code and message', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: { code: 'RELOCATE_STALE', message: 'path moved' } }), {
			status: 409,
			statusText: 'Conflict',
			headers: { 'content-type': 'application/json', etag: '"rev-2"' }
		});

	const caught = await clientModule.api.GET('/api/v1/health').then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof clientModule.ApiError, 'expected an ApiError');
	assert.equal(caught.status, 409);
	assert.equal(caught.code, 'RELOCATE_STALE');
	assert.equal(caught.message, 'path moved');
	assert.equal(caught.response.headers.get('etag'), '"rev-2"');
});

test('a bodyless error keeps the status, the statusText and the headers', async () => {
	globalThis.fetch = async () =>
		new Response(null, { status: 409, statusText: 'Conflict', headers: { etag: '"rev-3"' } });

	const caught = await clientModule.api.GET('/api/v1/health').then(
		() => null,
		(error) => error
	);

	assert.equal(caught.status, 409);
	assert.equal(caught.code, 'HTTP_409');
	assert.equal(caught.message, 'Conflict');
	assert.equal(caught.body, null);
	assert.equal(caught.response.headers.get('etag'), '"rev-3"');
});

test('a FastAPI string detail becomes the error message', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: 'Not Found' }), {
			status: 404,
			statusText: 'Not Found',
			headers: { 'content-type': 'application/json' }
		});

	const caught = await clientModule.api.GET('/api/v1/health').then(
		() => null,
		(error) => error
	);

	assert.equal(caught.code, 'HTTP_404');
	assert.equal(caught.message, 'Not Found');
});

test('a non-JSON error body does not mask the failure', async () => {
	globalThis.fetch = async () =>
		new Response('<html>502 upstream</html>', {
			status: 502,
			statusText: 'Bad Gateway',
			headers: { 'content-type': 'text/html' }
		});

	const caught = await clientModule.api.GET('/api/v1/health').then(
		() => null,
		(error) => error
	);

	assert.equal(caught.status, 502);
	assert.equal(caught.code, 'HTTP_502');
	assert.equal(caught.message, 'Bad Gateway');
	assert.equal(caught.body, null);
});

test('unwrap narrows a success body to a non-optional value', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ status: 'ok' }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});

	const health = await clientModule.unwrap(clientModule.api.GET('/api/v1/health'));

	assert.equal(health.status, 'ok');
});

test('unwrap fails loudly when a JSON route answers with no body', async () => {
	globalThis.fetch = async () => new Response(null, { status: 204 });

	const caught = await clientModule.unwrap(clientModule.api.GET('/api/v1/health')).then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof clientModule.ApiError, 'expected an ApiError');
	assert.equal(caught.code, 'EMPTY_BODY');
	assert.equal(caught.status, 204);
});

test('a transport failure propagates unwrapped so callers can classify it', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('fetch failed');
	};

	await assert.rejects(clientModule.api.GET('/api/v1/health'), /fetch failed/);
});
