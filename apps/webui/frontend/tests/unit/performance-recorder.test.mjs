import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://sets-api.example.test';
const iconRail = readFileSync(new URL('../../src/lib/components/rb/browser/IconRail.svelte', import.meta.url), 'utf8');
const performanceRecorderRail = readFileSync(new URL('../../src/lib/components/rb/browser/PerformanceRecorderRail.svelte', import.meta.url), 'utf8');
const browserPanel = readFileSync(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url), 'utf8');
let recorder;
let originalFetch;
let requests;

before(async () => {
	originalFetch = globalThis.fetch;
	requests = [];
	globalThis.fetch = async (request) => {
		const body = request.body === null ? null : await request.clone().text();
		requests.push({ url: request.url, method: request.method, body });
		return new Response(
			JSON.stringify({ active: true, session_id: '2026-09-05T12-00-00', pid: 42, owned: true, recoverable: false }),
			{ status: 201, headers: { 'content-type': 'application/json' } }
		);
	};
	recorder = await loadTypeScriptModule('src/lib/sets/performance-recorder.ts', { viteApiBase: API_BASE });
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('performance REC starts the verified recorder route with the Open DJ source enabled', async () => {
	requests.length = 0;
	await recorder.startPerformanceRecorder(3);

	assert.deepEqual(requests, [
		{
			url: `${API_BASE}/api/sets/recorder/start`,
			method: 'POST',
			body: JSON.stringify({
				session_id: null,
				ffmpeg_device_idx: 3,
				sources: ['djay_monitor', 'opendj_decks']
			})
		}
	]);
});

test('performance REC rejects an invalid device index before issuing a recording request', async () => {
	requests.length = 0;
	await assert.rejects(recorder.startPerformanceRecorder(-1), /non-negative integer/);
	assert.deepEqual(requests, []);
});

test('the live performance rail delegates its REC click to the verified recorder controller', () => {
	assert.doesNotMatch(iconRail, /recordings - future apps\/sets/);
	assert.match(iconRail, /action: 'record'/);
	assert.match(iconRail, /disabled=\{isInert \|\| \(isRecord && recordingBusy\)\}/);
	assert.match(iconRail, /isRecord \? onrecord : undefined/);
	assert.match(browserPanel, /<PerformanceRecorderRail \{source\} onspotify=\{selectSpotifySource\} \/>/);
	assert.match(performanceRecorderRail, /startPerformanceRecorder, stopPerformanceRecorder/);
	assert.match(performanceRecorderRail, /async function togglePerformanceRecording\(\): Promise<void>/);
	assert.match(performanceRecorderRail, /recorder = await getRecorderStatus\(\)/);
	assert.match(performanceRecorderRail, /recorder = await startPerformanceRecorder\(Number\(input\)\)/);
	assert.match(performanceRecorderRail, /recorder = await stopPerformanceRecorder\(recorder\)/);
	assert.match(performanceRecorderRail, /onrecord=\{\(\) => void togglePerformanceRecording\(\)\}/);
});
