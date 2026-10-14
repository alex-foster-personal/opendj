import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { FakeAudioContext, installWindow } from './fixtures/fake-web-audio.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * CMDK-04: a Cmd K pick waiting on rb-meta is dropped when its deck moves on.
 * The loads go through the production dispatcher (`dispatchPerformanceCommand`
 * with `type: 'load'`, which is what BrowserPanel's `_loadOntoDeck` sends)
 * into the production Web Audio engine module, and the pick's mark and commit
 * check are the bar's own (`deckLoadMark`, `loadCommitted`) reading that
 * engine's deck state.
 *
 * What this does NOT cover: node has no Web Audio, so the AudioContext is the
 * shared `FakeAudioContext` fixture, the stretch processor is the shared load
 * stub, and the daemon is the in-process route table below (the same harness
 * as quantize-default-on.test.mjs). Real decode, the AudioWorklet/DSP path and
 * the real daemon are UNAVAILABLE here; the browser-level race against the
 * real engine and fixture library is tracked on #6091. What this suite pins is
 * the deck-state bookkeeping the race turns on, which the engine module moves
 * itself (`load_generation` on a committed load, untouched on a refused one).
 *
 * Regression lines:
 * - if a load through the dispatcher does not move the mark then broken
 * - if a pick dispatches after another load took its deck then broken
 * - if a lone pick on an unchanged deck does not load once settled then broken
 * - if a refused load reports committed then broken
 */

const API_BASE = 'https://cmdk-deck-load.example.test';
const SID_A = 'a'.repeat(40);
const SID_B = 'b'.repeat(40);
const SID_MISSING = 'c'.repeat(40);
const STRETCH_STUB = fileURLToPath(new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url));
const realFetch = globalThis.fetch;

let m;
let uninstall = null;

function json(body, status = 200) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

/** The daemon routes a deck load reads, for two loadable tracks and one that 404s. */
function installDaemon() {
	const bands = { length: 0, low: [], mid: [], high: [] };
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		if (url.includes(SID_MISSING)) {
			return json({ detail: { code: 'TRACK_NOT_FOUND', message: 'no such track' } }, 404);
		}
		if (url.includes('/anlz?')) {
			return json({
				stable_id: url.match(/[0-9a-f]{40}/)[0],
				points: 38_400,
				waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
				beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] },
				beatgrid_source: 'rekordbox',
				cues: [],
				phrases: [],
				vocals: { status: 'not_analyzed' },
				local_waveform: { status: 'decoded' }
			});
		}
		if (url.endsWith('/hot-cues')) {
			return json(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({ slot, cue: null, revision: `empty-${slot}` })));
		}
		if (url.endsWith('/audio')) return new Response(new Uint8Array(4 * 48_000 * 4));
		const track = url.match(/\/tracks\/([0-9a-f]{40})(\/stems)?$/);
		if (track?.[2] !== undefined) return json({ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } }, 404);
		if (track !== null) return json({ stable_id: track[1], title: 'Cmd K', artist: 'Fixture', bpm: null });
		throw new Error(`unexpected request ${url}`);
	};
}

const deckState = (deck) => m.audio.deckStates[deck];
/** The browser's load: the dispatcher's `load`, with a refusal toasted, not thrown. */
const dispatchLoad = (deck, stable_id) =>
	m.ipc.dispatchPerformanceCommand({ type: 'load', deck, stable_id, suppressCommandErrorToast: true }).then(
		() => undefined,
		() => undefined
	);
const pendingRow = (stable_id) => ({ stable_id, file_exists: null, file_availability: 'AVAILABILITY_PENDING' });
const META = { file_exists: true, is_streaming: false, folder_path: '/music/a.mp3' };

/** The bar's pick loader, wired as CommandBarV2 wires it, with no browser reservations. */
function barPickLoader(fetchMeta, loaded) {
	return m.cmdk.createPickLoader(
		fetchMeta,
		(row, deck) =>
			m.cmdk.loadCommitted(() => deckState(deck), row.stable_id, async () => {
				loaded.push([row.stable_id, deck]);
				await dispatchLoad(deck, row.stable_id);
			}),
		(deck) => m.cmdk.deckLoadMark(deckState(deck), 0)
	);
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
	for (const toast of [...m.stores.toasts]) m.stores.dismissToast(toast.logId);
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
	delete globalThis.requestAnimationFrame;
	delete globalThis.cancelAnimationFrame;
	delete globalThis.window;
});

test('CMDK-04: a load through the dispatcher moves the deck mark, a refused one does not', async () => {
	const before = m.cmdk.deckLoadMark(deckState(1), 0);
	await dispatchLoad(1, SID_A);
	assert.equal(deckState(1).stable_id, SID_A, 'precondition: the track loaded');
	const loaded = m.cmdk.deckLoadMark(deckState(1), 0);
	assert.notEqual(loaded, before, 'if a load through the dispatcher does not move the mark then broken');
	// Reloading the same track still moves it (the generation, not just the track).
	await dispatchLoad(1, SID_A);
	assert.notEqual(m.cmdk.deckLoadMark(deckState(1), 0), loaded);
	// Control: a refused load leaves the mark where it was.
	const held = m.cmdk.deckLoadMark(deckState(1), 0);
	await dispatchLoad(1, SID_MISSING);
	assert.equal(deckState(1).stable_id, SID_A, 'precondition: the old track is still loaded');
	assert.equal(m.cmdk.deckLoadMark(deckState(1), 0), held);
});

test('CMDK-04: a pick waiting on rb-meta is dropped when a dispatcher load takes its deck first', async () => {
	let answer;
	const loaded = [];
	const pick = barPickLoader(() => new Promise((resolve) => (answer = resolve)), loaded);
	const waiting = pick(pendingRow(SID_A), 2);
	await dispatchLoad(2, SID_B);
	assert.equal(deckState(2).stable_id, SID_B, 'precondition: the other load landed');
	answer(META);
	assert.equal(await waiting, false, 'if a pick dispatches after another load took its deck then broken');
	assert.deepEqual(loaded, []);
	assert.equal(deckState(2).stable_id, SID_B, 'the newer load stays on the deck');
	// Control: a load on another deck leaves this deck's pick alone.
	const other = pick(pendingRow(SID_A), 2);
	await dispatchLoad(3, SID_B);
	answer(META);
	assert.equal(await other, true, 'if a load elsewhere drops this pick then broken');
	assert.deepEqual(loaded, [[SID_A, 2]]);
	assert.equal(deckState(2).stable_id, SID_A);
});

test('CMDK-04: a lone pick loads once settled, and a refused load reports not committed', async () => {
	let answer;
	const loaded = [];
	const pick = barPickLoader(() => new Promise((resolve) => (answer = resolve)), loaded);
	const lone = pick(pendingRow(SID_A), 1);
	answer(META);
	assert.equal(await lone, true, 'if a lone pick on an unchanged deck does not load once settled then broken');
	assert.equal(deckState(1).stable_id, SID_A);
	// The load path returns normally after refusing; the pick reports it did not commit.
	const refused = pick(pendingRow(SID_MISSING), 1);
	answer(META);
	assert.equal(await refused, false, 'if a refused load reports committed then broken');
	assert.deepEqual(loaded, [[SID_A, 1], [SID_MISSING, 1]]);
	assert.equal(deckState(1).stable_id, SID_A);
});
