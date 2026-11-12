// requirement: CTRL-10711
// Note 6 is LOOP IN and note 7 is LOOP OUT on both XDJ maps.
//   [if] note 6 then note 7 arrive on a loaded deck channel [then] a loop
//   engages from the first press position to the second
//   [if] note 7 arrives with no pending loop in [then] nothing is engaged and
//   the learn log says why

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

globalThis.location ??= { search: '' };

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let vite;
let webmidi;
let maps;
let glue;
let audioEngine;
let performanceIpc;
let uninstallPerformanceIpc;
let originalSetLoop;

function fakeInput(id, name) {
	return { id, name, manufacturer: 'Pioneer DJ', state: 'connected', onmidimessage: null };
}

const azIn = fakeInput('az-in', 'XDJ-AZ');
const fakeAccess = {
	inputs: new Map([[azIn.id, azIn]]),
	outputs: new Map(),
	onstatechange: null
};

function wire(status, d1, d2) {
	assert.notEqual(azIn.onmidimessage, null, 'XDJ-AZ got no onmidimessage listener');
	azIn.onmidimessage({ data: Uint8Array.of(status, d1, d2) });
}

function press(note) {
	// Deck 1 lives on channel 1: Note On 0x90.
	wire(0x90, note, 0x7f);
}

const setLoopCalls = [];

async function waitUntil(pred, label) {
	const deadline = Date.now() + 2000;
	while (!pred()) {
		if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
		await new Promise((resolve) => setImmediate(resolve));
	}
}

function loadDeck1(positionMs) {
	const deck = audioEngine.deckStates[1];
	deck.stable_id = 'loop-in-out-track';
	deck.playing = false;
	deck.position_ms = positionMs;
	deck.duration_ms = 60_000;
	deck.pending_loop_in_ms = null;
	deck.quantize_enabled = false;
	deck.loop = null;
}

before(async () => {
	const listeners = new Map();
	const store = new Map();
	globalThis.window = {
		addEventListener: (type, fn) => listeners.set(type, fn),
		removeEventListener: (type) => listeners.delete(type),
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	const request = async () => fakeAccess;
	try {
		globalThis.navigator.requestMIDIAccess = request;
	} catch {
		Object.defineProperty(globalThis, 'navigator', {
			value: { requestMIDIAccess: request },
			configurable: true
		});
	}

	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true, watch: null },
		plugins: [svelte()],
		resolve: {
			alias: { $lib: resolve(FRONTEND_ROOT, 'src/lib') },
			conditions: ['browser']
		}
	});
	webmidi = await vite.ssrLoadModule('/src/lib/rb/midi/webmidi.svelte.ts');
	maps = await vite.ssrLoadModule('/src/lib/rb/midi/maps/pioneer-xdj.ts');
	glue = await vite.ssrLoadModule('/src/lib/rb/midi/action-glue.svelte.ts');
	audioEngine = await vite.ssrLoadModule('/src/lib/rb/audio-engine.svelte.ts');
	performanceIpc = await vite.ssrLoadModule('/src/lib/rb/performance-ipc.svelte.ts');
	uninstallPerformanceIpc = performanceIpc.installPerformanceBrowserIpc();

	originalSetLoop = audioEngine.engine.setLoop.bind(audioEngine.engine);
	audioEngine.engine.setLoop = async (deck, loop, quantize) => {
		setLoopCalls.push({ deck, loop, quantize });
	};

	webmidi._resetMidiForTests();
	webmidi.registerDeviceMap(maps.XDJ_AZ_MAP);
	webmidi.registerDeviceMap(maps.XDJ_RX3_MAP);
	webmidi.registerActionHandler(glue.handleMidiAction);
	await webmidi.initMidi();
});

after(async () => {
	try {
		if (originalSetLoop !== undefined) audioEngine.engine.setLoop = originalSetLoop;
		uninstallPerformanceIpc?.();
		webmidi?._resetMidiForTests();
		delete globalThis.window;
	} finally {
		await vite?.close();
	}
});

test('note 6 then note 7 on a deck channel produces a loop from first press to second', async () => {
	setLoopCalls.length = 0;
	webmidi.learnLog.length = 0;
	loadDeck1(1000);

	press(6);
	await waitUntil(() => audioEngine.deckStates[1].pending_loop_in_ms === 1000, 'loop in point');
	assert.equal(setLoopCalls.length, 0, 'LOOP IN must not engage a loop');

	audioEngine.deckStates[1].position_ms = 4000;
	press(7);
	await waitUntil(() => setLoopCalls.length === 1, 'loop out setLoop');

	assert.deepEqual(setLoopCalls[0].deck, 1);
	assert.deepEqual(setLoopCalls[0].loop, { in_ms: 1000, out_ms: 4000 });
	assert.equal(audioEngine.deckStates[1].pending_loop_in_ms, null);
	assert.equal(webmidi.learnLog[0].mapped, true);
	assert.equal(webmidi.learnLog[0].action.type, 'deck_loop_out');
});

test('note 7 alone does nothing and the learn log says why', async () => {
	setLoopCalls.length = 0;
	webmidi.learnLog.length = 0;
	loadDeck1(2500);

	press(7);
	await waitUntil(() => webmidi.learnLog[0]?.mapped === false, 'loop out refusal in the learn log');
	assert.equal(setLoopCalls.length, 0);
	assert.equal(audioEngine.deckStates[1].pending_loop_in_ms, null);
	assert.equal(webmidi.learnLog[0].mapped, false);
	assert.match(webmidi.learnLog[0].note, /no loop in point|press LOOP IN first/);
	assert.equal(webmidi.learnLog[0].action.type, 'deck_loop_out');
});
