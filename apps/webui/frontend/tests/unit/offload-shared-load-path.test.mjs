import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { FakeAudioContext, installWindow } from './fixtures/fake-web-audio.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * requirement: OFFLOAD-8598h
 *
 * Every deck load restores an offloaded track first (the maintainer: "If I try to load
 * a track that is offloaded it should download it"; Codex P1 on #8601). The
 * load goes through the PRODUCTION dispatcher, `dispatchPerformanceCommand`,
 * which every surface uses: the browser, Quick Draw, drops, AutoPlay, the
 * agent bus behind `opendj load` and MCP. Real dispatcher, real Web Audio
 * engine on a fake AudioContext; only the daemon (`fetch`) is a double, and
 * its audio route refuses with 409 TRACK_OFFLOADED until the restore ran,
 * exactly as apps/adapters/rekordbox/paths.py does.
 *
 * - [if] a plain dispatcher load names an offloaded track [then] it is restored for 24h, then it is on the deck, [else stop].
 * - [if] AutoPlay hands an offloaded track to a deck [then] the same restore runs first, [else stop].
 * - [if] the track is local [then] the load reads the offload state once and restores nothing, [else stop].
 * - [if] an agent reads the performance state mid-download [then] it sees the deck's download and its op_id, [else stop].
 */

const API_BASE = 'https://offload-shared-load.example.test';
const SID_OFF = 'd'.repeat(40);
const SID_LOCAL = 'e'.repeat(40);
const STRETCH_STUB = fileURLToPath(new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url));
const realFetch = globalThis.fetch;

let m;
let uninstall = null;
let restored;
let midDownload;
let calls;

function json(body, status = 200) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function operation(stableId, state) {
	return {
		op_id: `op-${stableId.slice(0, 4)}`, kind: 'restore', mode: 'temporary', playlist_id: null, state,
		stable_ids: [stableId], tracks_total: 1, tracks_done: state === 'done' ? 1 : 0, bytes_total: 64,
		bytes_done: state === 'done' ? 64 : 0, bytes_freed: 0, current: null,
		results: state === 'done' ? { [stableId]: { state: 'temporary', note: null, error: null } } : {},
		error: null, dry_run: false
	};
}

function installDaemon() {
	const bands = { length: 0, low: [], mid: [], high: [] };
	globalThis.fetch = async (input, init) => {
		const request = input instanceof Request ? input : new Request(input, init);
		const url = new URL(request.url);
		calls.push(`${request.method} ${url.pathname}`);
		if (url.pathname === '/api/v1/offload/tracks') {
			const sid = url.searchParams.get('stable_ids');
			return json({ [sid]: sid === SID_OFF && !restored ? { state: 'offloaded', expires_at: null } : null });
		}
		if (url.pathname === '/api/v1/offload/restore') {
			assert.deepEqual(JSON.parse(await request.text()), { stable_ids: [SID_OFF], mode: 'temporary' });
			return json(operation(SID_OFF, 'running'), 202);
		}
		if (url.pathname.startsWith('/api/v1/offload/operations/')) {
			midDownload = m.ipc.queryPerformanceState().cloud_downloads;
			restored = true;
			return json(operation(SID_OFF, 'done'));
		}
		if (url.pathname.endsWith('/audio')) {
			if (url.pathname.includes(SID_OFF) && !restored) {
				return json({ detail: { code: 'TRACK_OFFLOADED', message: 'offloaded; restore it before loading' } }, 409);
			}
			return new Response(new Uint8Array(4 * 48_000 * 4));
		}
		if (url.pathname.endsWith('/anlz')) {
			return json({
				stable_id: url.pathname.match(/[0-9a-f]{40}/)[0], points: 38_400,
				waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
				beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] }, beatgrid_source: 'rekordbox',
				cues: [], phrases: [], vocals: { status: 'not_analyzed' }, local_waveform: { status: 'decoded' }
			});
		}
		if (url.pathname.endsWith('/hot-cues')) {
			return json(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({ slot, cue: null, revision: `empty-${slot}` })));
		}
		const track = url.pathname.match(/\/tracks\/([0-9a-f]{40})(\/stems)?$/);
		if (track?.[2] !== undefined) return json({ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } }, 404);
		if (track !== null) return json({ stable_id: track[1], title: 'Offload', artist: 'Fixture', bpm: null });
		throw new Error(`unexpected request ${url}`);
	};
}

before(async () => {
	installWindow('');
	globalThis.AudioContext = FakeAudioContext;
	globalThis.requestAnimationFrame = () => 0;
	globalThis.cancelAnimationFrame = () => {};
	m = await loadTypeScriptModule('tests/unit/fixtures/command-bar-v2-deck-load-entry.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/rb/stretch-adapter': STRETCH_STUB }
	});
});

beforeEach(async () => {
	await m.audio.engine.dispose();
	FakeAudioContext.instances = [];
	m.registry.resetAudioContextRegistryForTest();
	restored = false;
	calls = [];
	installDaemon();
	uninstall = m.ipc.installPerformanceBrowserIpc();
});

afterEach(() => {
	uninstall?.();
	uninstall = null;
	globalThis.fetch = realFetch;
});

after(async () => {
	await m.audio.engine.dispose();
	delete globalThis.AudioContext;
});

function restoreBeforeAudio() {
	const restore = calls.indexOf('POST /api/v1/offload/restore');
	const audio = calls.findIndex((c) => c.endsWith(`/${SID_OFF}/audio`));
	assert.ok(restore !== -1, `no restore was requested: ${calls.join(', ')}`);
	assert.ok(audio === -1 || restore < audio, 'the audio was fetched before the restore');
}

test('a dispatcher load of an offloaded track restores it, then the deck has it', async () => {
	await m.ipc.dispatchPerformanceCommand({ type: 'load', deck: 1, stable_id: SID_OFF });
	restoreBeforeAudio();
	assert.equal(m.audio.deckStates[1].stable_id, SID_OFF);
});

test('an AutoPlay hand-off of an offloaded track restores it first too', async () => {
	await m.ipc.dispatchPerformanceCommand({ type: 'load', deck: 2, stable_id: SID_OFF }, undefined, 'autoplay-handoff');
	restoreBeforeAudio();
	assert.equal(m.audio.deckStates[2].stable_id, SID_OFF);
});

test('a local track loads with one offload read and no restore', async () => {
	await m.ipc.dispatchPerformanceCommand({ type: 'load', deck: 3, stable_id: SID_LOCAL });
	assert.equal(calls.filter((c) => c.startsWith('GET /api/v1/offload/')).length, 1);
	assert.ok(!calls.some((c) => c.includes('/offload/restore')));
	assert.equal(m.audio.deckStates[3].stable_id, SID_LOCAL);
});

test('an agent reading the performance state mid-download sees the deck download', async () => {
	midDownload = null;
	await m.ipc.dispatchPerformanceCommand({ type: 'load', deck: 4, stable_id: SID_OFF });
	assert.deepEqual(midDownload[4], {
		stable_id: SID_OFF, op_id: `op-${SID_OFF.slice(0, 4)}`, state: 'running', bytes_done: 0, bytes_total: 64
	});
	assert.equal(midDownload[1], null);
	assert.equal(m.ipc.queryPerformanceState().cloud_downloads[4], null, 'cleared once the track is back');
});
