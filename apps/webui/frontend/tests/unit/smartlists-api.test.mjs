import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://smartlists-api.example.test';
let api;
let originalFetch;

before(async () => {
	api = await loadTypeScriptModule('src/lib/api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('smartlist tracks unwrap the live API response envelope', async () => {
	let requestedUrl = '';
	globalThis.fetch = async (request) => {
		requestedUrl = request.url;
		return Response.json({
			smartlist_id: 'smart / one',
			name: 'Late night',
			rule_summary: 'rating >= 4',
			order_by: 'rating desc',
			items: ['track-1'],
			tracks: [{ stable_id: 'track-1', title: 'Track one' }]
		});
	};

	const tracks = await api.getSmartlistTracks('smart / one');

	assert.equal(requestedUrl, `${API_BASE}/api/v1/smartlists/smart%20%2F%20one/tracks`);
	assert.deepEqual(tracks, [{ stable_id: 'track-1', title: 'Track one' }]);
});

test('smartlist list failures surface explicitly', async () => {
	globalThis.fetch = async () => Response.json({ detail: 'state unavailable' }, { status: 503 });

	await assert.rejects(() => api.listSmartlists(), /GET smartlists failed: 503/);
});

test('smartlist update sends the complete replacement and returns server readback', async () => {
	let seen;
	let sentBody;
	globalThis.fetch = async (request) => {
		seen = request;
		sentBody = await request.clone().json();
		return Response.json({
			id: 'smart / one',
			name: 'Late night',
			rule: { field: 'genre', op: 'in', value: ['drum, bass'] },
			order_by: 'rating desc',
			referenced_fields: ['genre'],
			last_evaluated_at: null,
			created_at: '2026-01-01T00:00:00+00:00',
			modified_at: '2026-01-01T00:00:01+00:00'
		}, { headers: { ETag: '"revision-b"' } });
	};

	const saved = await api.updateSmartlist('smart / one', {
		rule: { field: 'genre', op: 'in', value: ['drum, bass'] },
		order_by: 'rating desc'
	}, '"revision-a"');

	assert.equal(seen.url, `${API_BASE}/api/v1/smartlists/smart%20%2F%20one`);
	assert.equal(seen.method, 'PUT');
	assert.equal(seen.headers.get('if-match'), '"revision-a"');
	assert.deepEqual(sentBody, {
		rule: { field: 'genre', op: 'in', value: ['drum, bass'] },
		order_by: 'rating desc'
	});
	assert.equal(saved.smartlist.order_by, 'rating desc');
	assert.equal(saved.etag, '"revision-b"');
});

test('smartlist detail returns the opaque ETag needed for compare-and-swap', async () => {
	globalThis.fetch = async () => Response.json({
		id: 'smart-one',
		name: 'Late night',
		rule: { field: 'rating', op: '>=', value: 4 },
		order_by: 'rating desc',
		referenced_fields: ['rating'],
		last_evaluated_at: null,
		created_at: '2026-01-01T00:00:00+00:00',
		modified_at: '2026-01-01T00:00:00+00:00'
	}, { headers: { ETag: '"revision-a"' } });

	const detail = await api.getSmartlist('smart-one');

	assert.equal(detail.smartlist.id, 'smart-one');
	assert.equal(detail.etag, '"revision-a"');
});

test('smartlist detail fails fast when the server omits its ETag', async () => {
	globalThis.fetch = async () => Response.json({ id: 'smart-one' });

	await assert.rejects(() => api.getSmartlist('smart-one'), /response is missing ETag/);
});

test('stale smartlist update surfaces current summary and revision without retrying', async () => {
	globalThis.fetch = async () => Response.json({
		error: 'conflict',
		message: 'If-Match does not match the current row etag',
		current: {
			id: 'smart-one',
			name: 'Late night',
			rule: { field: 'energy', op: '>=', value: 7 },
			order_by: 'energy desc'
		},
		etag: '"revision-b"'
	}, { status: 409, headers: { ETag: '"revision-b"' } });

	await assert.rejects(
		() => api.updateSmartlist(
			'smart-one',
			{ rule: { field: 'rating', op: '>=', value: 4 } },
			'"revision-a"'
		),
		(error) => {
			assert.equal(error.constructor.name, 'SmartlistConflictError');
			assert.equal(error.etag, '"revision-b"');
			assert.equal(error.current.order_by, 'energy desc');
			return true;
		}
	);
});

test('smartlist editor preserves stale server state and exposes explicit recovery actions', async () => {
	const source = await readFile('src/routes/smartlists/[id]/+page.svelte', 'utf8');

	assert.match(source, /conflictCurrent = exc\.current/);
	assert.match(source, /etag = exc\.etag/);
	assert.match(source, /Current saved rule:/);
	assert.match(source, /onclick=\{reloadConflict\}/);
	assert.match(source, /onclick=\{retryConflict\}/);
});
