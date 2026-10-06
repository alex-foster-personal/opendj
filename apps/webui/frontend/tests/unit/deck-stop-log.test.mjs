import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { FakeAudioContext, installWindow } from './fixtures/fake-web-audio.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * PLAY-18 (CORE, Tue 6 Oct 2026): every deck STOP logs ONE structured line with
 * its cause, and the deck's `last_stop` says whether a person stopped it.
 * Soak round 4: the 05:19:57-05:21:06Z stop had zero log evidence.
 *
 * Regression lines:
 * - if a UI pause does not log cause=user-ui with user_pause true then broken
 * - if an agent order pause does not log cause=agent-command then broken
 * - if an AutoPlay dispatch stop does not log cause=autoplay-handoff then broken
 * - if a track playing out does not log cause=end-of-track then broken
 * - if the engine stop (silence-dropout source) does not log cause=engine then broken
 * - if tearing the engine down mid-play does not log cause=reload then broken
 * - if one stop logs two lines, or a stop with no known cause is guessed, then broken
 */

const API_BASE = 'https://deck-stop-log.example.test';
const SID = 'd'.repeat(40);
const STRETCH_STUB = fileURLToPath(new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url));
/** One minute of audio by default; the end-of-track test swaps in one second. */
const MINUTE_BYTES = 4 * 48_000 * 60;
const SECOND_BYTES = 4 * 48_000;
let trackBytes = MINUTE_BYTES;
const realFetch = globalThis.fetch;
const realSetTimeout = globalThis.setTimeout;
const realSetInterval = globalThis.setInterval;

let m;
let uninstallIpc = null;
let frameTimer = null;
const frames = new Map();
let nextFrame = 0;

class LiveClockAudioContext extends FakeAudioContext {
	#startedAtMs = performance.now();
	get currentTime() { return (performance.now() - this.#startedAtMs) / 1000; }
	set currentTime(_value) { /* derived from the wall clock */ }
	getOutputTimestamp() { return { contextTime: this.currentTime, performanceTime: performance.now() }; }
	createConstantSource() {
		const node = super.createConstantSource();
		node.stop = (when = 0) => {
			realSetTimeout(() => node.onended?.(), Math.max(0, (when - this.currentTime) * 1000));
		};
		return node;
	}
}

function json(body, status = 200) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function installDaemon() {
	const bands = { length: 0, low: [], mid: [], high: [] };
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		if (url.includes('/anlz?')) {
			return json({
				stable_id: SID, points: 38_400,
				waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
				beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] }, beatgrid_source: 'rekordbox',
				cues: [], phrases: [], vocals: { status: 'not_analyzed' }, local_waveform: { status: 'decoded' }
			});
		}
		if (url.endsWith('/hot-cues')) {
			return json(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({ slot, cue: null, revision: `empty-${slot}` })));
		}
		if (url.endsWith('/audio')) return new Response(new Uint8Array(trackBytes));
		if (url.endsWith('/stems')) return json({ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } }, 404);
		if (url.endsWith('/rb-meta')) return json({ detail: { code: 'RB_META_NOT_FOUND', message: 'no rekordbox row' } }, 404);
		if (url.endsWith(`/tracks/${SID}`)) return json({ stable_id: SID, title: 'Stop', artist: 'Fixture', bpm: null });
		throw new Error(`unexpected request ${url}`);
	};
}

const sleep = (ms) => new Promise((resolve) => realSetTimeout(resolve, ms));

async function until(predicate, what, timeoutMs = 4000) {
	const deadline = performance.now() + timeoutMs;
	while (performance.now() < deadline) {
		if (predicate()) return;
		await sleep(10);
	}
	assert.fail(`${what} within ${timeoutMs} ms`);
}

const deck = () => m.audio.getDeckState(1);
const stopLines = () => m.perf.readPerfEvents().filter((event) => event.kind === 'deck-stop');

async function loadAndPlay() {
	await m.ipc.dispatchPerformanceCommand({ type: 'load', deck: 1, stable_id: SID });
	await m.ipc.dispatchPerformanceCommand({ type: 'play', deck: 1, playing: true });
	await until(() => deck().playing, 'the deck plays');
}

before(async () => {
	installWindow('');
	globalThis.AudioContext = LiveClockAudioContext;
	globalThis.requestAnimationFrame = (callback) => {
		nextFrame += 1;
		frames.set(nextFrame, callback);
		return nextFrame;
	};
	globalThis.cancelAnimationFrame = (handle) => frames.delete(handle);
	frameTimer = realSetInterval(() => {
		const due = [...frames.values()];
		frames.clear();
		for (const callback of due) callback(performance.now());
	}, 16);
	m = await loadTypeScriptModule('tests/unit/fixtures/deck-stop-log-entry.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/rb/stretch-adapter': STRETCH_STUB }
	});
});

beforeEach(async () => {
	await m.audio.engine.dispose();
	FakeAudioContext.instances = [];
	m.registry.resetAudioContextRegistryForTest();
	for (const toast of [...m.stores.toasts]) m.stores.dismissToast(toast.logId);
	trackBytes = MINUTE_BYTES;
	installDaemon();
	m.perf.resetPerfEventLog();
	m.stopLog.resetDeckStopLogForTest();
	uninstallIpc = m.ipc.installPerformanceBrowserIpc();
});

afterEach(() => {
	uninstallIpc?.();
	uninstallIpc = null;
	globalThis.fetch = realFetch;
});

after(async () => {
	await m.audio.engine.dispose();
	clearInterval(frameTimer);
	delete globalThis.AudioContext;
	delete globalThis.requestAnimationFrame;
	delete globalThis.cancelAnimationFrame;
	delete globalThis.window;
});

test('deckStopCause: engine origins win over a running command, the command source next, else unattributed', async () => {
	const { deckStopCause, withDeckCommandSource } = m.stopLog;
	assert.equal(deckStopCause(1, 'other'), 'unattributed', 'a stop with no known cause is said, never guessed');
	await withDeckCommandSource([1], 'agent-command', async () => {
		assert.equal(deckStopCause(1, 'command'), 'agent-command');
		assert.equal(deckStopCause(2, 'command'), 'unattributed', 'the register is per deck');
		assert.equal(deckStopCause(1, 'natural-end'), 'end-of-track', 'a track ending mid-command is still the track ending');
		assert.equal(deckStopCause(1, 'worklet'), 'engine');
		assert.equal(deckStopCause(1, 'dropout'), 'engine');
		await withDeckCommandSource([1], 'autoplay-handoff', async () => {
			assert.equal(deckStopCause(1, 'command'), 'autoplay-handoff');
		});
		assert.equal(deckStopCause(1, 'command'), 'agent-command', 'nesting restores the outer source');
	});
	assert.equal(deckStopCause(1, 'command'), 'unattributed');
});

test('a UI pause logs one line with cause user-ui and last_stop.user_pause true', async () => {
	await loadAndPlay();
	m.perf.resetPerfEventLog();
	const result = await m.ipc.runPerformanceCommandFromUi({ type: 'play', deck: 1, playing: false });
	assert.deepEqual(result, { ok: true });
	const lines = stopLines();
	assert.equal(lines.length, 1, 'if one stop logs zero or two lines then broken');
	assert.equal(lines[0].deck, 1);
	assert.match(lines[0].message, /^cause=user-ui seq=1 position_ms=\d+ stable_id=d{40}$/);
	const stop = m.stopLog.readLastDeckStop(1);
	assert.equal(stop.cause, 'user-ui');
	assert.equal(stop.user_pause, true);
	assert.equal(stop.stable_id, SID);
});

test('an agent order pause logs cause agent-command and is not a user pause', async () => {
	await loadAndPlay();
	const order = await m.executeAgentOrder({ kind: 'single', payload: { type: 'play', deck: 1, playing: false } });
	assert.deepEqual(order.steps, [{ status: 'succeeded' }]);
	const stop = m.stopLog.readLastDeckStop(1);
	assert.equal(stop.cause, 'agent-command');
	assert.equal(stop.user_pause, false);
});

test('the IPC bridge is an agent command too', async () => {
	await loadAndPlay();
	await globalThis.window.musicDjToolsPerformance.dispatch({ type: 'play', deck: 1, playing: false });
	assert.equal(m.stopLog.readLastDeckStop(1).cause, 'agent-command');
});

test('a stop AutoPlay dispatches logs cause autoplay-handoff', async () => {
	await loadAndPlay();
	await m.ipc.dispatchPerformanceCommand({ type: 'unload', deck: 1 }, undefined, 'autoplay-handoff');
	assert.equal(m.stopLog.readLastDeckStop(1).cause, 'autoplay-handoff');
});

test('the silence-dropout act logs cause engine; any other in-app dispatch logs app-command', async () => {
	await loadAndPlay();
	await m.ipc.dispatchPerformanceCommand({ type: 'play', deck: 1, playing: false }, undefined, 'engine');
	assert.equal(m.stopLog.readLastDeckStop(1).cause, 'engine');
	await m.ipc.dispatchPerformanceCommand({ type: 'play', deck: 1, playing: true });
	await until(() => deck().playing, 'the deck plays again');
	await m.ipc.dispatchPerformanceCommand({ type: 'play', deck: 1, playing: false });
	const stop = m.stopLog.readLastDeckStop(1);
	assert.equal(stop.cause, 'app-command');
	assert.equal(stop.seq, 2, 'seq counts this deck\'s stops');
});

test('a track playing out logs cause end-of-track', async () => {
	trackBytes = SECOND_BYTES;
	await loadAndPlay();
	await until(() => !deck().playing, 'the one-second track plays out', 6000);
	const stop = m.stopLog.readLastDeckStop(1);
	assert.equal(stop?.cause, 'end-of-track', 'if a played-out track is not attributed to its end then broken');
	assert.equal(stopLines().length, 1);
});

test('tearing the engine down mid-play logs cause reload', async () => {
	await loadAndPlay();
	await m.audio.engine.dispose();
	assert.equal(m.stopLog.readLastDeckStop(1).cause, 'reload');
});
