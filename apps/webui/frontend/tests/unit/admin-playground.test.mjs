/**
 * Admin Playground tab helpers.
 *
 * Regression lines:
 * - tab=playground resolves to playground; missing/unknown still kpi
 * - /api/v1/health and /api/v1/tracks?limit=10 parse; bad paths throw and do not call fetch
 * - GET with a body textarea still sends no body
 * - POST with invalid JSON throws before fetch
 * - HTTP 404 is a result object with status === 404, not a thrown Error
 * - well-formed SQL body parses; a string columns throws; HTTP 503 surfaces status + body
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://admin-playground.example.test';

let adminTab;
let playgroundApi;
let originalFetch;
let fetchCalls;

before(async () => {
	adminTab = await loadTypeScriptModule('src/routes/admin/admin-tab.ts');
	playgroundApi = await loadTypeScriptModule('src/routes/admin/playground-api.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

function stubFetch(handler) {
	fetchCalls = [];
	globalThis.fetch = async (request, init) => {
		fetchCalls.push({ request, init });
		return handler(request, init);
	};
}

test('missing tab query resolves to kpi', () => {
	assert.equal(adminTab.adminTabFromUrl(new URL('https://x.test/admin')), 'kpi');
});

test('unknown tab query resolves to kpi', () => {
	assert.equal(adminTab.adminTabFromUrl(new URL('https://x.test/admin?tab=other')), 'kpi');
});

// REQ: ADMIN-02
test('tab=playground resolves to playground', () => {
	assert.equal(
		adminTab.adminTabFromUrl(new URL('https://x.test/admin?tab=playground')),
		'playground'
	);
});

test('parsePlaygroundPath accepts valid /api/v1 paths', () => {
	assert.equal(playgroundApi.parsePlaygroundPath('/api/v1/health'), '/api/v1/health');
	assert.equal(
		playgroundApi.parsePlaygroundPath('/api/v1/tracks?limit=10'),
		'/api/v1/tracks?limit=10'
	);
});

test('parsePlaygroundPath rejects unsafe paths without calling fetch', async () => {
	for (const bad of [
		'/health',
		'/api/v1health',
		'https://evil.test/api/v1/health',
		'//evil.test/api/v1/health',
		'/api/v1/../secret'
	]) {
		stubFetch(() => {
			throw new Error('fetch must not run for invalid path');
		});
		await assert.rejects(
			() =>
				playgroundApi.sendPlaygroundRequest({
					method: 'GET',
					path: bad,
					bodyText: ''
				}),
			/Error/
		);
		assert.equal(fetchCalls.length, 0);
	}
});

// REQ: ADMIN-02
test('GET sends no body even when bodyText is non-empty', async () => {
	stubFetch(async () =>
		new Response('ok', { status: 200, headers: { 'content-type': 'text/plain' } })
	);
	await playgroundApi.sendPlaygroundRequest({
		method: 'GET',
		path: '/api/v1/health',
		bodyText: '{"x":1}'
	});
	assert.equal(fetchCalls.length, 1);
	assert.equal(fetchCalls[0].init?.body, undefined);
});

test('POST with invalid JSON throws before fetch', async () => {
	stubFetch(() => {
		throw new Error('fetch must not run for invalid JSON');
	});
	await assert.rejects(
		() =>
			playgroundApi.sendPlaygroundRequest({
				method: 'POST',
				path: '/api/v1/health',
				bodyText: '{not json'
			}),
		/body is not valid JSON/
	);
	assert.equal(fetchCalls.length, 0);
});

test('HTTP 404 is returned as a result object', async () => {
	stubFetch(async () =>
		new Response('missing', { status: 404, headers: { 'content-type': 'text/plain' } })
	);
	const result = await playgroundApi.sendPlaygroundRequest({
		method: 'GET',
		path: '/api/v1/nope',
		bodyText: ''
	});
	assert.equal(result.status, 404);
	assert.equal(result.body, 'missing');
});

test('parseSqlQueryResult accepts a well-formed body', () => {
	const parsed = playgroundApi.parseSqlQueryResult({
		columns: ['id'],
		rows: [[1]],
		truncated: false,
		row_count: 1
	});
	assert.deepEqual(parsed.columns, ['id']);
	assert.equal(parsed.row_count, 1);
});

test('parseSqlQueryResult rejects string columns', () => {
	assert.throws(
		() => playgroundApi.parseSqlQueryResult({ columns: 'id', rows: [], truncated: false, row_count: 0 }),
		/not a string array/
	);
});

test('formatSqlCell renders null literally', () => {
	assert.equal(playgroundApi.formatSqlCell(null), 'null');
});

test('fetchSqlQuery surfaces HTTP 503 with body text', async () => {
	stubFetch(async () =>
		new Response(
			JSON.stringify({ detail: { code: 'sql_db_unavailable', message: 'missing db' } }),
			{ status: 503, headers: { 'content-type': 'application/json' } }
		)
	);
	await assert.rejects(
		() => playgroundApi.fetchSqlQuery('SELECT 1', 200),
		/POST \/api\/v1\/admin\/sql-query failed \(HTTP 503\):/
	);
});
