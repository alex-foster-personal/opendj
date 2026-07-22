import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://sets-api.example.test';
let api;
let originalFetch;
let requests;

before(async () => {
	api = await loadTypeScriptModule('src/routes/sets/sets-api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
	requests = [];
	globalThis.fetch = async (input, init) => {
		requests.push({ url: String(input), method: init?.method ?? 'GET', body: init?.body ?? null });
		return new Response(JSON.stringify({ active: false, session_id: null, pid: null }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('all REC controls use the configured HTTP API contract', async () => {
	await api.getRecorderStatus();
	await api.startRecorder({ session_id: null, ffmpeg_device_idx: 2, sources: ['djay_monitor'] });
	await api.stopRecorder('session / one');

	assert.deepEqual(requests, [
		{ url: `${API_BASE}/api/sets/recorder`, method: 'GET', body: null },
		{
			url: `${API_BASE}/api/sets/recorder/start`,
			method: 'POST',
			body: JSON.stringify({ session_id: null, ffmpeg_device_idx: 2, sources: ['djay_monitor'] })
		},
		{
			url: `${API_BASE}/api/sets/recorder/session%20%2F%20one/stop`,
			method: 'POST',
			body: null
		}
	]);
});

test('session, timeline, and replay URLs are HTTP-addressable', () => {
	assert.equal(api.sessionAudioUrl('session / one', 'audio one.mp3'),
		`${API_BASE}/api/sets/session%20%2F%20one/audio/audio%20one.mp3`);
});
