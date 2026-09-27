// requirement: IOPIN-12
//
// The output-stall rebuild (issue #2155: a new AudioContext with the loaded
// decks re-attached) is the one graph build that runs while decks are loaded.
// Every case here drives the armed recovery itself (rebind, then the engine's
// recreate), the same entry the liveness poll's `stalled` verdict calls.
//
// [if] a master output selection is parked on setSinkId across a rebuild
//   [then] it rejects as stale and publishes nothing [⛔️ if the rebuild is
//   handed the failed-build headphone release, which retires nothing]
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { FakeAudioContext, installWindow } from './fixtures/fake-web-audio.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://iopin-12-stall-rebuild.example.test';
const SID = 'c'.repeat(40);
const SID_2 = 'd'.repeat(40);
// node:test cannot host the Signalsmith worklet; this stand-in keeps the load
// and re-attach paths real up to the processor boundary.
const STRETCH_STUB = fileURLToPath(new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url));

let audio;
let stores;
let player;
let registry;
let instrumentation;
let stretch;
const realFetch = globalThis.fetch;
const savedNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');

//-----------------------------------------------------------------------------
// daemon HTTP double: the smallest valid answers the deck-load path reads,
// shaped like deck-load-no-analysis-path.test.mjs
//-----------------------------------------------------------------------------

function json(body, status = 200) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function installDaemon() {
	const bands = { length: 0, low: [], mid: [], high: [] };
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
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
		if (url.endsWith('/audio')) return new Response(new Uint8Array(4 * 4800));
		const track = url.match(/\/tracks\/([0-9a-f]{40})(\/stems)?$/);
		if (track?.[2] !== undefined) return json({ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } }, 404);
		if (track !== null) return json({ stable_id: track[1], title: 'Stall Rebuild', artist: 'Fixture', bpm: null });
		throw new Error(`unexpected request ${url}`);
	};
}

//-----------------------------------------------------------------------------
// harness
//-----------------------------------------------------------------------------

before(async () => {
	installWindow('');
	globalThis.AudioContext = FakeAudioContext;
	// The engine's transport clock runs on animation frames; play() starts it.
	globalThis.requestAnimationFrame = (callback) => setTimeout(() => callback(performance.now()), 16);
	globalThis.cancelAnimationFrame = (handle) => clearTimeout(handle);
	const entry = await loadTypeScriptModule('tests/unit/fixtures/djio-fallback-entry.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/rb/stretch-adapter': STRETCH_STUB }
	});
	({ audio, stores, player, registry, instrumentation, stretch } = entry);
});

beforeEach(async () => {
	await audio.engine.dispose();
	FakeAudioContext.maxChannelCount = 2;
	FakeAudioContext.failWhen = null;
	FakeAudioContext.instances = [];
	// Module-level, like `instances`: each case counts only the contexts it made.
	registry.resetAudioContextRegistryForTest();
	for (const toast of [...stores.toasts]) stores.dismissToast(toast.logId);
	installDaemon();
});

afterEach(() => {
	globalThis.fetch = realFetch;
});

after(async () => {
	await audio.engine.dispose();
	for (const toast of [...stores.toasts]) stores.dismissToast(toast.logId);
	delete globalThis.AudioContext;
	delete globalThis.requestAnimationFrame;
	delete globalThis.cancelAnimationFrame;
	delete globalThis.window;
});

//-----------------------------------------------------------------------------
// the rebuild still retires in-flight output selections
//-----------------------------------------------------------------------------

test('IOPIN-12 control: a master output selection parked across a stall rebuild rejects as stale and publishes nothing', async () => {
	// The overshoot of the failed-build fix: a failed build must not retire the
	// selection it runs in, but a rebuild still must, or a sink that lands after
	// the old route is gone publishes onto the rebuilt one.
	installWindow('');
	const mediaDevices = { enumerateDevices: async () => [], addEventListener() {}, removeEventListener() {} };
	Object.defineProperty(globalThis, 'navigator', { value: { mediaDevices }, configurable: true, writable: true });
	player.mixerState.headphones.outputs = [{ id: 'dev-a', label: 'dev-a' }];
	let landSink = null;
	FakeAudioContext.prototype.setSinkId = () => new Promise((resolve) => { landSink = resolve; });
	try {
		const pending = audio.engine.selectMasterOutput('dev-a');
		assert.equal(typeof landSink, 'function', 'precondition: the selection is parked on setSinkId');
		const parkedOn = FakeAudioContext.instances.at(-1);
		await instrumentation._recoverOutputStallForTests();
		assert.notEqual(FakeAudioContext.instances.at(-1), parkedOn, 'precondition: the rebuild replaced the context under the selection');
		landSink();
		await assert.rejects(
			pending,
			/stale headphone operation/,
			'if the rebuild stops retiring in-flight selections then a late sink publishes onto the rebuilt route - broken'
		);
		assert.equal(player.mixerState.headphones.selected_master_output_device_id, null, 'the retired selection published nothing');
	} finally {
		delete FakeAudioContext.prototype.setSinkId;
		if (savedNavigator === undefined) delete globalThis.navigator;
		else Object.defineProperty(globalThis, 'navigator', savedNavigator);
		const hp = player.mixerState.headphones;
		hp.outputs = [];
		hp.error = null;
		hp.selected_master_output_device_id = null;
	}
});
