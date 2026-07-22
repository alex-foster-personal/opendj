import assert from 'node:assert/strict';
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
	globalThis.fetch = async (input) => {
		requestedUrl = String(input);
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
