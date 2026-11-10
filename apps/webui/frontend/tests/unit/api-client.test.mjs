/**
 * Contract tests for the one generated-OpenAPI client (src/lib/api/client.ts).
 *
 * These pin the plumbing every converted module inherits: the base URL, the
 * always-throw middleware, and the {"detail": {code, message}} decoding.
 */
import assert from 'node:assert/strict';
import { execSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const REPO_ROOT = fileURLToPath(new URL('../../../../../', import.meta.url));
const FRONTEND_SRC = fileURLToPath(new URL('../../src', import.meta.url));

const CLIENT_IMPORT =
	/from\s+['"](?:\$lib\/api\/client|\.\.?\/\.\.?\/api\/client|\.\.?\/api\/client)['"]/;

function walkSourceFiles(dir, acc = []) {
	for (const name of readdirSync(dir)) {
		const path = join(dir, name);
		const st = statSync(path);
		if (st.isDirectory()) {
			if (name !== 'sandbox') walkSourceFiles(path, acc);
			continue;
		}
		if (/\.(ts|svelte)$/.test(name)) acc.push(path);
	}
	return acc;
}

function clientImportNames(source) {
	const names = [];
	for (const line of source.split('\n')) {
		if (!CLIENT_IMPORT.test(line)) continue;
		const match = line.match(/import\s+(?:type\s+)?\{([^}]+)\}/);
		if (!match) continue;
		for (const part of match[1].split(',')) {
			const trimmed = part.trim().replace(/^type\s+/, '');
			const id = trimmed.split(/\s+as\s+/)[0].trim();
			if (id) names.push(id);
		}
	}
	return names;
}

function readFrontend(rel) {
	return readFileSync(join(FRONTEND_SRC, rel), 'utf8');
}

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
	const payload = JSON.stringify({ detail: { code: 'RELOCATE_STALE', message: 'path moved' } });
	assert.equal(caught.bodyText, payload);
	assert.deepEqual(caught.body, JSON.parse(payload));
	assert.equal(caught.response.bodyUsed, true);
});

test('a bodyless error keeps the status, the statusText and the headers', async () => {
	globalThis.fetch = async () =>
		new Response('', { status: 409, statusText: 'Conflict', headers: { etag: '"rev-3"' } });

	const caught = await clientModule.api.GET('/api/v1/health').then(
		() => null,
		(error) => error
	);

	assert.equal(caught.status, 409);
	assert.equal(caught.code, 'HTTP_409');
	assert.equal(caught.message, 'Conflict');
	assert.equal(caught.body, null);
	assert.equal(caught.bodyText, '');
	assert.equal(caught.response.headers.get('etag'), '"rev-3"');
	assert.equal(caught.response.bodyUsed, true);
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
	assert.equal(caught.bodyText, '<html>502 upstream</html>');
	assert.equal(caught.response.bodyUsed, true);
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

test('requireBody hands back both halves so a caller can read a header', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ status: 'ok' }), {
			status: 200,
			headers: { 'content-type': 'application/json', etag: '"rev-7"' }
		});

	const { data, response } = clientModule.requireBody(await clientModule.api.GET('/api/v1/health'));

	assert.equal(data.status, 'ok');
	assert.equal(response.headers.get('etag'), '"rev-7"');
});

test('requireBody fails loudly when a JSON route answers with no body', async () => {
	// The reason it exists: a call site that also wants the ETag used to
	// destructure { data, response } straight off the client and assert the
	// body onto its type, so an empty body reached UI state as undefined.
	globalThis.fetch = async () => new Response(null, { status: 204 });

	const call = await clientModule.api.GET('/api/v1/health');
	let caught = null;
	try {
		clientModule.requireBody(call);
	} catch (error) {
		caught = error;
	}

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

// Issue #11211 acceptance (split http-instance / unwrap / domain entries)

test('[if] a module imports only ApiError from api/client.ts [then] fail, [else stop].', () => {
	const offenders = [];
	for (const file of walkSourceFiles(FRONTEND_SRC)) {
		const names = clientImportNames(readFileSync(file, 'utf8'));
		if (names.length === 1 && names[0] === 'ApiError') offenders.push(file);
	}
	assert.deepEqual(offenders, []);
});

test('[if] a module imports only API_BASE from api/client.ts [then] fail, [else stop].', () => {
	const offenders = [];
	for (const file of walkSourceFiles(FRONTEND_SRC)) {
		const names = clientImportNames(readFileSync(file, 'utf8'));
		if (names.length === 1 && names[0] === 'API_BASE') offenders.push(file);
	}
	assert.deepEqual(offenders, []);
});

test('[if] unwrap or requireBody is defined in client.ts not unwrap.ts [then] fail, [else stop].', () => {
	const clientSrc = readFrontend('lib/api/client.ts');
	const unwrapSrc = readFrontend('lib/api/unwrap.ts');
	assert.doesNotMatch(clientSrc, /export async function unwrap/);
	assert.doesNotMatch(clientSrc, /export function requireBody/);
	assert.match(unwrapSrc, /export async function unwrap/);
	assert.match(unwrapSrc, /export function requireBody/);
});

test('[if] createFetchClient is called outside http-instance.ts [then] fail, [else stop].', () => {
	const hits = [];
	for (const file of walkSourceFiles(join(FRONTEND_SRC, 'lib'))) {
		if (file.endsWith('http-instance.ts')) continue;
		if (readFileSync(file, 'utf8').includes('createFetchClient')) hits.push(file);
	}
	assert.deepEqual(hits, []);
});

test('[if] client.ts is deleted or frontend.max_fan_in drifts [then] fail, [else stop].', () => {
	assert.ok(existsSync(join(FRONTEND_SRC, 'lib/api/client.ts')));
	const onMain = execSync('git show origin/main:ops/quality/baseline.json', {
		encoding: 'utf8',
		cwd: REPO_ROOT
	});
	const local = readFileSync(join(REPO_ROOT, 'ops/quality/baseline.json'), 'utf8');
	const mainFanIn = JSON.parse(onMain).metrics['frontend.max_fan_in'];
	const localFanIn = JSON.parse(local).metrics['frontend.max_fan_in'];
	assert.equal(localFanIn, mainFanIn);
});

test('[if] domain HTTP entry modules are missing exports [then] fail, [else stop].', async () => {
	const paths = ['rb-http.ts', 'admin-http.ts', 'app-http.ts'];
	for (const name of paths) {
		const mod = await loadTypeScriptModule(`src/lib/api/${name}`);
		for (const key of ['api', 'unwrap', 'requireBody', 'API_BASE', 'ApiError']) {
			assert.ok(key in mod, `${name} must export ${key}`);
		}
	}
	const instance = await loadTypeScriptModule('src/lib/api/http-instance.ts', {
		viteApiBase: API_BASE
	});
	assert.equal(typeof instance.api.GET, 'function');
});
