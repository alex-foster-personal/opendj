import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://sets-api.example.test';
let api;
let originalFetch;
let requests;

const exportPayload = {
	kind: 'metadata_only',
	session_id: 'session / one',
	audio_upload: 'not_offered',
	takeover: 'not_offered',
	rights_position: 'unsettled',
	licensing_reminder: 'You must own the rights.',
	tracklist: [],
	comment: '0:00 Artist - Title\n'
};

before(async () => {
	api = await loadTypeScriptModule('src/routes/sets/sets-api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
	requests = [];
	globalThis.fetch = async (request) => {
		const body = request.body === null ? null : await request.clone().text();
		requests.push({ url: request.url, method: request.method, body });
		return new Response(JSON.stringify(exportPayload), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('SoundCloud export GET and POST URLs encode session ids', async () => {
	requests.length = 0;
	await api.getSoundcloudExport('session / one');
	await api.acknowledgeSoundcloudExport('session / one');

	assert.deepEqual(requests, [
		{
			url: `${API_BASE}/api/sets/session%20%2F%20one/soundcloud-export`,
			method: 'GET',
			body: null
		},
		{
			url: `${API_BASE}/api/sets/session%20%2F%20one/soundcloud-export`,
			method: 'POST',
			body: JSON.stringify({ acknowledge_rights: true })
		}
	]);
});
