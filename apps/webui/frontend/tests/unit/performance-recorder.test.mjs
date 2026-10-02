import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://sets-api.example.test';
const iconRail = readFileSync(new URL('../../src/lib/components/rb/browser/IconRail.svelte', import.meta.url), 'utf8');
const performanceRecorderRail = readFileSync(new URL('../../src/lib/components/rb/browser/PerformanceRecorderRail.svelte', import.meta.url), 'utf8');
const recordInputPicker = readFileSync(new URL('../../src/lib/components/rb/browser/RecordInputPicker.svelte', import.meta.url), 'utf8');
const browserPanel = readFileSync(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url), 'utf8');
let recorder;
let choice;
let originalFetch;
let requests;
let rememberedBody = '{"remembered":null}';

before(async () => {
	originalFetch = globalThis.fetch;
	requests = [];
	globalThis.fetch = async (request) => {
		const body = request.body === null ? null : await request.clone().text();
		requests.push({ url: request.url, method: request.method, body });
		if (request.url.endsWith('/api/sets/recorder/remembered-input')) {
			return new Response(rememberedBody, { status: 200, headers: { 'content-type': 'application/json' } });
		}
		return new Response(
			JSON.stringify({ active: true, session_id: '2026-09-05T12-00-00', pid: 42, owned: true, recoverable: false }),
			{ status: 201, headers: { 'content-type': 'application/json' } }
		);
	};
	recorder = await loadTypeScriptModule('src/lib/sets/performance-recorder.ts', { viteApiBase: API_BASE });
	choice = await loadTypeScriptModule('src/lib/sets/record-input-choice.ts', { viteApiBase: API_BASE });
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('performance REC starts the recorder on the picked input BY NAME with the Open DJ source enabled', async () => {
	requests.length = 0;
	await choice.startPerformanceRecorder({ kind: 'device', name: 'BlackHole 2ch' });

	assert.deepEqual(requests, [
		{
			url: `${API_BASE}/api/sets/recorder/start`,
			method: 'POST',
			body: JSON.stringify({
				session_id: null,
				device_name: 'BlackHole 2ch',
				capture_audio: true,
				sources: ['djay_monitor', 'opendj_decks']
			})
		}
	]);
});

test('performance REC tracklist-only start says so explicitly instead of omitting the input', async () => {
	requests.length = 0;
	await choice.startPerformanceRecorder({ kind: 'none' });

	assert.equal(requests.length, 1);
	assert.deepEqual(JSON.parse(requests[0].body), {
		session_id: null,
		capture_audio: false,
		sources: ['djay_monitor', 'opendj_decks']
	});
});

test('the picked input is read back from the daemon, not browser storage that forgets per port', async () => {
	requests.length = 0;
	rememberedBody = JSON.stringify({ remembered: null });
	assert.equal(await choice.getRememberedInput(), null);
	rememberedBody = JSON.stringify({ remembered: { kind: 'device', name: 'Loopback Audio' } });
	assert.deepEqual(await choice.getRememberedInput(), { kind: 'device', name: 'Loopback Audio' });
	rememberedBody = JSON.stringify({ remembered: { kind: 'none', name: null } });
	assert.deepEqual(await choice.getRememberedInput(), { kind: 'none' });
	assert.deepEqual(
		requests.map((r) => [r.method, r.url]),
		Array(3).fill(['GET', `${API_BASE}/api/sets/recorder/remembered-input`])
	);
	assert.equal('rememberInput' in choice, false);
});

test('the picker opens on the remembered input while connected, else the loopback default, never a mic', () => {
	const devices = {
		devices: [
			{ index: 0, name: 'MacBook Pro Microphone', loopback: false },
			{ index: 2, name: 'BlackHole 2ch', loopback: true }
		],
		default_name: 'BlackHole 2ch'
	};
	const mic = { kind: 'device', name: 'MacBook Pro Microphone' };
	assert.deepEqual(choice.initialInputChoice(mic, devices), mic);
	assert.deepEqual(choice.initialInputChoice({ kind: 'device', name: 'Unplugged' }, devices), {
		kind: 'device',
		name: 'BlackHole 2ch'
	});
	assert.deepEqual(choice.initialInputChoice(null, devices), { kind: 'device', name: 'BlackHole 2ch' });
	assert.equal(choice.initialInputChoice(null, { devices: devices.devices.slice(0, 1), default_name: null }), null);
	assert.deepEqual(choice.initialInputChoice({ kind: 'none' }, null), { kind: 'none' });
	assert.equal(choice.initialInputChoice(mic, null), null);
});

test('the live performance rail opens the in-app input picker instead of window.prompt', () => {
	assert.doesNotMatch(iconRail, /recordings - future apps\/sets/);
	assert.match(iconRail, /action: 'record'/);
	assert.match(iconRail, /disabled=\{isInert \|\| \(isRecord && recordingBusy\)\}/);
	assert.match(iconRail, /isRecord \? onrecord : undefined/);
	assert.match(browserPanel, /<PerformanceRecorderRail \{source\} onspotify=\{selectSpotifySource\} \/>/);
	assert.doesNotMatch(performanceRecorderRail, /window\.prompt/);
	assert.match(performanceRecorderRail, /async function togglePerformanceRecording\(\): Promise<void>/);
	assert.match(performanceRecorderRail, /recorder = await getRecorderStatus\(\)/);
	assert.match(recordInputPicker, /const status = await startPerformanceRecorder\(choice\)/);
	assert.match(performanceRecorderRail, /onstarted=\{\(status\) => \(\(recorder = status\), \(RecordInputPicker = null\)\)\}/);
	assert.match(recordInputPicker, /getRememberedInput\(\)/);
	assert.doesNotMatch(recordInputPicker, /localStorage/);
	assert.match(performanceRecorderRail, /<RecordInputPicker/);
	assert.match(performanceRecorderRail, /import\('.\/RecordInputPicker.svelte'\)/);
	assert.match(performanceRecorderRail, /recorder = await stopPerformanceRecorder\(recorder\)/);
	assert.match(performanceRecorderRail, /onrecord=\{\(\) => void togglePerformanceRecording\(\)\}/);
	assert.match(recordInputPicker, /listRecorderDevices\(\)/);
	assert.match(recordInputPicker, /Tracklist only \(no audio\)/);
});
