import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://sets-api.example.test';
let api;
let originalFetch;
let requests;

before(async () => {
	api = await loadTypeScriptModule('src/routes/sets/sets-api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
	requests = [];
	// openapi-fetch calls fetch(request) with one Request object.
	globalThis.fetch = async (request) => {
		const body = request.body === null ? null : await request.clone().text();
		requests.push({ url: request.url, method: request.method, body });
		return new Response(JSON.stringify({ active: false, session_id: null, pid: null, owned: false, recoverable: false }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('all REC controls use the configured HTTP API contract', async () => {
	requests.length = 0;
	await api.getRecorderStatus();
	await api.startRecorder({ session_id: null, ffmpeg_device_idx: 2, sources: ['djay_monitor'] });
	await api.stopRecorder('session / one');
	await api.recoverRecorder('session / one', 42);

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
		},
		{
			url: `${API_BASE}/api/sets/recorder/session%20%2F%20one/recover`,
			method: 'POST',
			body: JSON.stringify({ expected_pid: 42 })
		}
	]);
});

test('session, timeline, and replay URLs are HTTP-addressable', () => {
	assert.equal(api.sessionAudioUrl('session / one', 'audio one.mp3'),
		`${API_BASE}/api/sets/session%20%2F%20one/audio/audio%20one.mp3`);
});

test('metadata-only set shares use the agent-addressable HTTP contract', async () => {
	requests.length = 0;
	await api.publishMetadataShare('session / one');
	await api.getMetadataShare('session / one');

	assert.deepEqual(requests, [
		{
			url: `${API_BASE}/api/sets/session%20%2F%20one/share`,
			method: 'POST',
			body: JSON.stringify({ confirm_metadata_only: true })
		},
		{
			url: `${API_BASE}/api/sets/session%20%2F%20one/share`,
			method: 'GET',
			body: null
		}
	]);
});

test('the public share route is server-rendered rather than loading the full client application', () => {
	const pagePath = fileURLToPath(new URL('../../../../sets/share_page.py', import.meta.url));
	const source = readFileSync(pagePath, 'utf8');

	assert.match(source, /@router.get\("\/sets\/shared\/\{session_id\}"/);
	assert.match(source, /track_loaded/);
	assert.match(source, /Metadata-only share/);
	assert.doesNotMatch(source, /mp3_segments|<audio/);
});

test('non-2xx maps detail string or statusText onto Error', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: 'recorder busy' }), {
			status: 409,
			statusText: 'Conflict',
			headers: { 'content-type': 'application/json' }
		});
	await assert.rejects(api.getRecorderStatus(), (error) => {
		assert.equal(error.message, 'recorder busy');
		return true;
	});

	globalThis.fetch = async () =>
		new Response(null, {
			status: 503,
			statusText: 'Service Unavailable'
		});
	await assert.rejects(api.getRecorderStatus(), (error) => {
		assert.equal(error.message, '503 Service Unavailable');
		return true;
	});
});

test('getTimeline parses NDJSON and rejects a malformed line', async () => {
	const eventA = {
		session_id: 's1',
		timestamp_s: 1,
		wall_clock: '2026-01-01T00:00:01Z',
		action: 'play',
		source: 'rb',
		deck: 'A',
		track_stable_id: 't1',
		value: {}
	};
	const eventB = {
		session_id: 's1',
		timestamp_s: 2,
		wall_clock: '2026-01-01T00:00:02Z',
		action: 'stop',
		source: 'rb',
		deck: 'A',
		track_stable_id: 't1',
		value: { reason: 'end' }
	};

	let seenUrl = '';
	globalThis.fetch = async (request) => {
		seenUrl = request.url;
		return new Response(`${JSON.stringify(eventA)}\n${JSON.stringify(eventB)}\n`, {
			status: 200,
			headers: { 'content-type': 'application/x-ndjson' }
		});
	};

	const events = await api.getTimeline('session / one');
	assert.equal(seenUrl, `${API_BASE}/api/sets/session%20%2F%20one/timeline`);
	assert.deepEqual(events, [eventA, eventB]);

	globalThis.fetch = async () =>
		new Response(`${JSON.stringify(eventA)}\n{not-json}\n`, {
			status: 200,
			headers: { 'content-type': 'application/x-ndjson' }
		});
	await assert.rejects(api.getTimeline('session / one'), /Malformed timeline event at line 2/);
});
