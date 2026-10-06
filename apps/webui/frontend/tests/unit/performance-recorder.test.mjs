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

test('performance REC starts the recorder on the picked input BY NAME with an explicit source', async () => {
	requests.length = 0;
	await choice.startPerformanceRecorder({ kind: 'device', name: 'BlackHole 2ch' }, 'loopback');
	await choice.startPerformanceRecorder({ kind: 'device', name: 'Scarlett 2i2' }, 'midi');

	assert.deepEqual(
		requests.map((r) => [r.url, r.method, JSON.parse(r.body)]),
		[
			[
				`${API_BASE}/api/sets/recorder/start`,
				'POST',
				{
					session_id: null,
					sources: ['djay_monitor', 'opendj_decks'],
					source: 'loopback',
					device_name: 'BlackHole 2ch'
				}
			],
			[
				`${API_BASE}/api/sets/recorder/start`,
				'POST',
				{
					session_id: null,
					sources: ['djay_monitor', 'opendj_decks'],
					source: 'external',
					device_name: 'Scarlett 2i2'
				}
			]
		]
	);
});

test('performance REC master-mix and tracklist-only starts name their source, never an input', async () => {
	requests.length = 0;
	await choice.startPerformanceRecorder({ kind: 'master' }, 'machine');
	await choice.startPerformanceRecorder({ kind: 'none' }, 'midi');

	assert.deepEqual(
		requests.map((r) => JSON.parse(r.body)),
		[
			{ session_id: null, sources: ['djay_monitor', 'opendj_decks'], source: 'master' },
			{ session_id: null, sources: ['djay_monitor', 'opendj_decks'], source: 'none' }
		]
	);
});

test('the picked source is read back from the daemon, not browser storage that forgets per port', async () => {
	requests.length = 0;
	rememberedBody = JSON.stringify({ remembered: null });
	assert.equal(await choice.getRememberedInput(), null);
	rememberedBody = JSON.stringify({ remembered: { kind: 'device', name: 'Loopback Audio' } });
	assert.deepEqual(await choice.getRememberedInput(), { kind: 'device', name: 'Loopback Audio' });
	rememberedBody = JSON.stringify({ remembered: { kind: 'none', name: null } });
	assert.deepEqual(await choice.getRememberedInput(), { kind: 'none' });
	rememberedBody = JSON.stringify({ remembered: { kind: 'master', name: null } });
	assert.deepEqual(await choice.getRememberedInput(), { kind: 'master' });
	assert.deepEqual(
		requests.map((r) => [r.method, r.url]),
		Array(4).fill(['GET', `${API_BASE}/api/sets/recorder/remembered-input`])
	);
	assert.equal('rememberInput' in choice, false);
});

const DEVICES = {
	devices: [
		{ index: 0, name: 'MacBook Pro Microphone', loopback: false },
		{ index: 1, name: 'Scarlett 2i2', loopback: false },
		{ index: 2, name: 'BlackHole 2ch', loopback: true }
	],
	default_name: 'BlackHole 2ch'
};

test('the toggle is MACHINE | LOOPBACK | MIDI and each view lists only its inputs', () => {
	assert.deepEqual(
		choice.RECORD_MODES.map((m) => [m.mode, m.label]),
		[
			['machine', 'MACHINE'],
			['loopback', 'LOOPBACK'],
			['midi', 'MIDI']
		]
	);
	assert.deepEqual(choice.devicesForMode(DEVICES, 'machine'), []);
	assert.deepEqual(choice.devicesForMode(DEVICES, 'loopback').map((d) => d.name), ['BlackHole 2ch']);
	assert.deepEqual(choice.devicesForMode(DEVICES, 'midi').map((d) => d.name), [
		'MacBook Pro Microphone',
		'Scarlett 2i2'
	]);
	assert.deepEqual(choice.devicesForMode(null, 'loopback'), []);
	assert.match(choice.INSTALL_LOOPBACK_URL, /^https:\/\//);
});

test('the picker opens on MACHINE by default, the remembered choice while it can be made, never a mic', () => {
	const mic = { kind: 'device', name: 'MacBook Pro Microphone' };
	const sel = choice.initialRecordSelection;
	// Default: the master mix, needing no driver (SET-12).
	assert.deepEqual(sel(null, DEVICES, true), { mode: 'machine', choice: { kind: 'master' } });
	assert.deepEqual(sel(null, null, true), { mode: 'machine', choice: { kind: 'master' } });
	assert.deepEqual(sel({ kind: 'master' }, DEVICES, true), { mode: 'machine', choice: { kind: 'master' } });
	// Remembered inputs open in their own view.
	assert.deepEqual(sel(mic, DEVICES, true), { mode: 'midi', choice: mic });
	assert.deepEqual(sel({ kind: 'device', name: 'BlackHole 2ch' }, DEVICES, true), {
		mode: 'loopback',
		choice: { kind: 'device', name: 'BlackHole 2ch' }
	});
	assert.deepEqual(sel({ kind: 'none' }, null, true), { mode: 'midi', choice: { kind: 'none' } });
	assert.deepEqual(sel({ kind: 'device', name: 'Unplugged' }, DEVICES, true), {
		mode: 'machine',
		choice: { kind: 'master' }
	});
	// Rust engine: no master tap, so the loopback default, and never a mic.
	assert.deepEqual(sel({ kind: 'master' }, DEVICES, false), {
		mode: 'loopback',
		choice: { kind: 'device', name: 'BlackHole 2ch' }
	});
	assert.deepEqual(sel(null, { devices: DEVICES.devices.slice(0, 2), default_name: null }, false), {
		mode: 'loopback',
		choice: null
	});
	assert.equal(choice.defaultChoiceForMode('midi', DEVICES, true), null);
	assert.equal(choice.defaultChoiceForMode('machine', DEVICES, false), null);
	assert.match(choice.masterMixUnavailableReason(true), /Rust engine/);
	assert.equal(choice.masterMixUnavailableReason(false), null);
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
	assert.match(recordInputPicker, /const status = await startPerformanceRecorder\(choice, mode\)/);
	assert.match(performanceRecorderRail, /onstarted=\{\(status\) => \(setRecorder\(status\), \(RecordInputPicker = null\)\)\}/);
	assert.match(recordInputPicker, /getRememberedInput\(\)/);
	assert.doesNotMatch(recordInputPicker, /localStorage/);
	assert.match(recordInputPicker, /if \(remembered\.status === 'rejected'\) rememberError = reason\(remembered\.reason\)/);
	assert.match(recordInputPicker, /data-testid="record-input-remember-error"/);
	assert.match(performanceRecorderRail, /<RecordInputPicker/);
	assert.match(performanceRecorderRail, /import\('.\/RecordInputPicker.svelte'\)/);
	assert.match(performanceRecorderRail, /await stopMasterTap\(\);\s*setRecorder\(await stopPerformanceRecorder\(recorder\)\)/);
	// SET-12, found live: a status poll landing mid-stop attached a second tap.
	assert.match(performanceRecorderRail, /if \(stopping \|\| master !== null/);
	assert.match(performanceRecorderRail, /onrecord=\{\(\) => void togglePerformanceRecording\(\)\}/);
	assert.match(recordInputPicker, /listRecorderDevices\(\)/);
	assert.match(recordInputPicker, /Tracklist only \(no audio\)/);
});

test('REC lights only once audio is written, not while the macOS microphone prompt is up', () => {
	const base = { active: true, session_id: 's', pid: 1, owned: true, recoverable: false };
	const waiting = recorder.recordRailState({ ...base, capture: 'waiting_permission' });
	assert.equal(waiting.recording, false);
	assert.equal(waiting.waiting, true);
	assert.equal(waiting.poll, 1000);
	assert.match(waiting.tip, /Waiting for microphone permission/);
	assert.equal(recorder.recordRailState({ ...base, capture: 'starting' }).recording, false);
	const failed = recorder.recordRailState({ ...base, capture: 'failed' });
	assert.equal(failed.recording, false);
	assert.equal(failed.poll, null);
	assert.match(failed.tip, /stopped recording/);
	// Controls: audio being written, and a tracklist-only recording, light REC.
	for (const capture of ['recording', 'none', 'unknown']) {
		const state = recorder.recordRailState({ ...base, capture });
		assert.deepEqual([state.recording, state.waiting, state.tip], [true, false, null], capture);
	}
	// Codex P1 (PR #5164): a capture that is recording is still watched, so an
	// input unplugged mid-set reaches 'failed' and unlights REC; one with no
	// capture of ours (tracklist only, another process) is not polled.
	assert.equal(recorder.recordRailState({ ...base, capture: 'recording' }).poll, 3000);
	assert.equal(recorder.recordRailState({ ...base, capture: 'none' }).poll, null);
	assert.equal(recorder.recordRailState({ ...base, capture: 'unknown' }).poll, null);
	assert.equal(recorder.recordRailState({ ...base, active: false, capture: 'none' }).recording, false);
	// SET-12: idle REC still re-reads, so an agent-started master recording gets its tap.
	assert.equal(recorder.recordRailState({ ...base, active: false, capture: 'none' }).poll, recorder.IDLE_POLL_MS);
});

test('the rail lights REC from the capture state and polls while it is waiting', () => {
	assert.match(performanceRecorderRail, /recording=\{rail\.recording\}/);
	assert.match(performanceRecorderRail, /recordingWaiting=\{rail\.waiting\}/);
	assert.match(performanceRecorderRail, /const after = rail\.poll;\s*if \(after === null\) return;/);
	assert.match(performanceRecorderRail, /setTimeout\(\(\) => void refreshRecorderStatus\(\), after\)/);
	assert.match(iconRail, /class:waiting=\{isRecord && recordingWaiting\}/);
});

test('a capture that fails says why when the engine said, on both REC surfaces', () => {
	const base = { active: true, session_id: 's', pid: 1, owned: true, recoverable: false };
	const why = 'microphone access for Open DJ is off; turn it on in System Settings';
	assert.match(recorder.captureFailureMessage({ ...base, capture: 'failed', capture_error: why }), /System Settings/);
	assert.match(recorder.captureFailureMessage({ ...base, capture: 'failed', capture_error: null }), /audio input stopped\. Press REC/);
	// Controls: no failure, no message.
	assert.equal(recorder.captureFailureMessage({ ...base, capture: 'recording', capture_error: null }), null);
	assert.equal(recorder.captureFailureMessage({ ...base, active: false, capture: 'none' }), null);
	assert.match(performanceRecorderRail, /captureFailureMessage\(recorder\)/);
});

test('the /sets panel reads the capture state, not just active, and polls it', () => {
	const base = { active: true, session_id: 's', pid: 1, owned: true, recoverable: false };
	assert.equal(recorder.recorderHeadline({ ...base, capture: 'waiting_permission' }), 'Waiting for microphone permission');
	assert.equal(recorder.recorderHeadline({ ...base, capture: 'starting' }), 'Starting the audio input');
	assert.equal(recorder.recorderHeadline({ ...base, capture: 'failed' }), 'Audio input stopped');
	assert.equal(recorder.recorderHeadline({ ...base, capture: 'recording' }), 'Recording');
	assert.equal(recorder.recorderHeadline({ ...base, active: false, capture: 'none' }), 'Recorder ready');
	const page = readFileSync(new URL('../../src/routes/sets/+page.svelte', import.meta.url), 'utf8');
	assert.match(page, /class:live=\{recorder\.active && rail\.recording\}/);
	assert.match(page, /<strong>\{recorderHeadline\(recorder\)\}<\/strong>/);
	assert.match(page, /setTimeout\(\(\) => void refreshRecorder\(\), after\)/);
	assert.match(page, /captureFailureMessage\(recorder\)/);
});
