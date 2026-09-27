// requirement: IOPIN-12
//
// The output-stall rebuild (issue #2155: a new AudioContext with the loaded
// decks re-attached) is the one graph build that runs while decks are loaded.
// Every case here drives the armed recovery itself (rebind, then the engine's
// recreate), the same entry the liveness poll's `stalled` verdict calls.
//
// [if] the rebuild's new graph cannot be built (an ?extroute page whose
//   interface dropped to 2 channels) [then] every deck it detached is unloaded
//   with the cause on the deck, and a load on a fixed output works [⛔️ if a
//   deck keeps its track while play() rejects "no track loaded"]
// [if] closing the OLD graph rejects during the rebuild [then] the graph state
//   is still reset, so the next load builds a fresh graph [⛔️ if every later
//   load fails "audio graph is missing" for the rest of the session]
// [if] the rebuild succeeds [then] the loaded deck keeps its track and gets a
//   processor on the new context, and only that context stays registered
//   (control: the unload above must not overshoot)
// [if] the rebuild re-attaches deck 1 and then fails on deck 2 [then] only deck
//   2 is unloaded (control: a deck that got its processor back keeps its track)
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

const recreateFailedToast = () => stores.toasts.find((toast) => /graph recreate failed/.test(toast.message));

/** Load deck 1 for real and prove it holds its track before a case rebuilds under it. */
async function loadDeckOne() {
	await audio.engine.load(1, SID);
	assert.equal(audio.getDeckState(1).stable_id, SID, 'precondition: deck 1 is loaded');
	assert.equal(audio.getDeckState(1).processor_error, null, 'precondition: deck 1 has no processor error');
}

//-----------------------------------------------------------------------------
// a failed rebuild never leaves a deck that shows a track it cannot play
//-----------------------------------------------------------------------------

test('IOPIN-12: a rebuild whose new graph cannot be built unloads the deck it detached, names the cause, and the next load plays', async () => {
	installWindow('?extroute=1:3');
	FakeAudioContext.maxChannelCount = 4;
	await loadDeckOne();
	// The interface drops to a 2-channel device; extroute still needs 4, so the
	// rebuild's own graph build throws with no injected failure.
	FakeAudioContext.maxChannelCount = 2;
	await instrumentation._recoverOutputStallForTests();
	assert.ok(recreateFailedToast(), 'precondition: the recreate really failed and said so');
	assert.equal(audio.gigDeckGraphIsPresent(), false, 'precondition: the failed rebuild was discarded');
	const deck = audio.getDeckState(1);
	assert.equal(
		deck.stable_id,
		null,
		'if the deck keeps its track after its processor is gone then the deck shows a track play() rejects as "no track loaded" - broken'
	);
	assert.match(deck.processor_error ?? '', /audio graph recreate failed/, 'if the deck carries no error then the operator sees an empty deck with no reason - broken');
	assert.match(deck.processor_error ?? '', /extroute needs 4 output channels/, 'the deck names the build error that caused it');
	assert.match(deck.processor_error ?? '', /reload the track/, 'the deck names the fix');
	assert.equal(registry.countRegisteredAudioContexts(), 0, 'neither the replaced context nor the failed one stays registered');

	FakeAudioContext.maxChannelCount = 4;
	await audio.engine.load(1, SID);
	assert.equal(audio.getDeckState(1).stable_id, SID, 'the next load on a fixed output works');
	assert.equal(audio.getDeckState(1).processor_error, null, 'and clears the rebuild error');
	await audio.engine.play(1);
	assert.equal(audio.getDeckState(1).playing, true, 'and the reloaded deck plays');
});

test('IOPIN-12: a rebuild whose teardown of the old graph rejects still resets the graph, so the next load builds a fresh one', async () => {
	installWindow('');
	await loadDeckOne();
	const old = FakeAudioContext.instances.at(-1);
	// A browser that rejects close() on the stalled context: the teardown throws
	// BEFORE the rebuild ever reaches its own graph build.
	old.close = async () => {
		throw new Error('InvalidStateError: the stalled context refused to close');
	};
	try {
		await instrumentation._recoverOutputStallForTests();
		assert.ok(recreateFailedToast(), 'precondition: the recreate failed on the teardown and said so');
		assert.equal(
			audio.gigDeckGraphIsPresent(),
			false,
			'if the old context stays installed with its deck nodes cleared then every later load fails "audio graph is missing" - broken'
		);
		assert.equal(audio.getDeckState(1).stable_id, null, 'the deck the rebuild detached is unloaded, not left showing a dead track');
		assert.match(audio.getDeckState(1).processor_error ?? '', /refused to close/);

		await audio.engine.load(1, SID);
		assert.equal(audio.getDeckState(1).stable_id, SID, 'if the next load fails then the deck is dead for the rest of the session - broken');
		assert.notEqual(FakeAudioContext.instances.at(-1), old, 'the next load built a new context');
	} finally {
		// Only this case's close refuses; a later case's teardown must not trip on it.
		delete old.close;
	}
});

test('IOPIN-12 control: a rebuild that succeeds keeps the loaded deck, re-attaches it on the new context, and registers only that context', async () => {
	installWindow('');
	await loadDeckOne();
	const old = FakeAudioContext.instances.at(-1);
	await instrumentation._recoverOutputStallForTests();
	assert.equal(recreateFailedToast(), undefined, 'precondition: the recreate succeeded');
	assert.notEqual(FakeAudioContext.instances.at(-1), old, 'precondition: the rebuild made a new context');
	assert.equal(audio.getDeckState(1).stable_id, SID, 'if a successful rebuild unloads decks then every stall recovery ejects the set - broken');
	assert.equal(audio.getDeckState(1).processor_error, null);
	await audio.engine.play(1);
	assert.equal(audio.getDeckState(1).playing, true, 'the re-attached deck plays on the new context');
	assert.equal(
		registry.countRegisteredAudioContexts(),
		1,
		'if the replaced context stays registered then library mode reports a leaked context after every stall recovery (PERFMODE-14) - broken'
	);
});

test('IOPIN-12 control: a rebuild that re-attaches deck 1 and then fails on deck 2 unloads deck 2 only', async () => {
	installWindow('');
	await loadDeckOne();
	await audio.engine.load(2, SID_2);
	assert.equal(audio.getDeckState(2).stable_id, SID_2, 'precondition: deck 2 is loaded');
	const realCreate = stretch.StretchDeckProcessor.create;
	let creates = 0;
	// The re-attach makes one processor per loaded deck, deck 1 first. Deck 2's
	// fails the way a worklet that cannot start on the new context does.
	stretch.StretchDeckProcessor.create = async (...args) => {
		creates += 1;
		if (creates === 2) throw new Error('AudioWorkletNode could not start on the new context');
		return realCreate.apply(stretch.StretchDeckProcessor, args);
	};
	try {
		await instrumentation._recoverOutputStallForTests();
	} finally {
		stretch.StretchDeckProcessor.create = realCreate;
	}
	assert.equal(creates, 2, 'precondition: the rebuild re-attached deck 1 and failed on deck 2');
	assert.ok(recreateFailedToast(), 'precondition: the recreate failed and said so');
	assert.equal(audio.getDeckState(1).stable_id, SID, 'if a deck the rebuild DID re-attach is unloaded too then one failed deck ejects the whole set - broken');
	assert.equal(audio.getDeckState(1).processor_error, null);
	assert.equal(audio.getDeckState(2).stable_id, null, 'the deck left without a processor is unloaded');
	assert.match(audio.getDeckState(2).processor_error ?? '', /could not start on the new context/);
	await audio.engine.play(1);
	assert.equal(audio.getDeckState(1).playing, true, 'the re-attached deck still plays');
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
