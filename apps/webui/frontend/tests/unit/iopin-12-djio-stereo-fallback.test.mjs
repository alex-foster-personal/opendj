// requirement: IOPIN-12
//
// Observed live on the installed Preview build d92bf93f (Fri 25 Sep 2026,
// 21:27Z): the Mixtour Pro map redirected the page to ?djio=master12-cue34
// while macOS output was the 2-channel MacBook Air speakers. The deck-2 load
// failed with "djio master12-cue34 needs four output channels", the NEXT load
// failed with "load: deck 2 audio graph is missing", and no deck loaded again
// for the rest of the page session.
//
// Two defects, tested separately:
//   1. A djio request on an output with < 4 channels threw instead of
//      degrading to stereo master.
//   2. ANY throw during graph construction left `_ctx` assigned with no deck
//      nodes, so every later `_ensureGraph()` returned that half-built context
//      and every later load hit "audio graph is missing" forever.
//
// [if] ?djio=master12-cue34 and the output exposes 2 channels [then] the graph
//   builds as stereo master, every deck is wired, and a copyable warn toast plus
//   status names the Mixtour + reload fix [⛔️ if it throws or any deck is unwired]
// [if] the output exposes 4 channels [then] djio still wires master 1/2 + cue
//   3/4 (control: a fix that always falls back to stereo fails here)
// [if] graph construction throws once [then] the next build starts from a fresh
//   context and every deck is wired [⛔️ if the half-built context is reused]
// [if] the page is already on ?djio= (including a stereo-fallback page) or on
//   ?extroute= [then] the controller redirect does not fire again [⛔️ if
//   location.replace can loop or produce a URL whose graph cannot build]
// [if] the shell's MIDI boot runs twice before the redirect lands [then] it
//   replaces once and subscribes nothing on the leaving page [⛔️ if a Tauri
//   listener survives into the reload and doubles every press]
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, beforeEach, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const DJIO = 'master12-cue34';

//-----------------------------------------------------------------------------
// Web Audio test double: node:test has no AudioContext. Every node records its
// edges on the owning context so the tests can assert what reached the
// destination. It fabricates no app data; it stands in for the browser API.
//-----------------------------------------------------------------------------

class FakeParam {
	value = 0;
	setValueAtTime(value) { this.value = value; return this; }
	setTargetAtTime(value) { this.value = value; return this; }
	linearRampToValueAtTime(value) { this.value = value; return this; }
	exponentialRampToValueAtTime(value) { this.value = value; return this; }
	cancelScheduledValues() { return this; }
	cancelAndHoldAtTime() { return this; }
}

function fakeNode(ctx, kind, params = [], extra = {}) {
	const node = {
		kind,
		context: ctx,
		channelCount: 2,
		channelCountMode: 'max',
		channelInterpretation: 'speakers',
		connect(to, output = 0, input = 0) {
			// The real API throws InvalidAccessError across contexts, which is
			// how a stale node from a discarded graph would surface.
			if (to.kind !== undefined && to.context !== ctx) {
				throw new Error(`InvalidAccessError: ${node.kind} -> ${to.kind} crosses AudioContexts`);
			}
			ctx.edges.push({ from: node, to, output, input });
			return to;
		},
		disconnect() {
			ctx.disconnected.add(node);
		},
		addEventListener() {},
		removeEventListener() {},
		...extra
	};
	for (const name of params) node[name] = new FakeParam();
	return node;
}

class FakeAudioContext {
	/** Channels the fake output device exposes, read by each NEW context. */
	static maxChannelCount = 2;
	/** Optional injected construction failure: (ctx, kind, nthCallOfKind) => boolean. */
	static failWhen = null;
	static instances = [];

	constructor(options) {
		this.options = options;
		this.state = 'running';
		this.sampleRate = 48000;
		this.baseLatency = 256 / 48000;
		this.outputLatency = 0.02;
		this.currentTime = 0;
		this.edges = [];
		this.disconnected = new Set();
		this.closed = false;
		this.calls = new Map();
		this.destination = fakeNode(this, 'destination', [], {
			maxChannelCount: FakeAudioContext.maxChannelCount
		});
		this.audioWorklet = {
			addModule: async () => {
				throw new Error('AudioWorklet is not available under node:test');
			}
		};
		FakeAudioContext.instances.push(this);
	}

	_create(kind, params, extra) {
		const nth = (this.calls.get(kind) ?? 0) + 1;
		this.calls.set(kind, nth);
		if (FakeAudioContext.failWhen?.(this, kind, nth) === true) {
			throw new Error(`injected ${kind} construction failure`);
		}
		return fakeNode(this, kind, params, extra);
	}

	createGain() { return this._create('gain', ['gain']); }
	createAnalyser() {
		return this._create('analyser', [], {
			fftSize: 2048,
			frequencyBinCount: 1024,
			getFloatTimeDomainData() {},
			getByteTimeDomainData() {},
			getFloatFrequencyData() {}
		});
	}
	createDelay(maxDelayTime) { return this._create('delay', ['delayTime'], { maxDelayTime }); }
	createBiquadFilter() { return this._create('biquad', ['frequency', 'Q', 'gain', 'detune'], { type: 'lowpass' }); }
	createChannelSplitter(outputs = 6) { return this._create('splitter', [], { numberOfOutputs: outputs }); }
	createChannelMerger(inputs = 6) { return this._create('merger', [], { numberOfInputs: inputs }); }
	createStereoPanner() { return this._create('panner', ['pan']); }
	createConstantSource() { return this._create('constant', ['offset'], { start() {}, stop() {} }); }
	createOscillator() { return this._create('oscillator', ['frequency', 'detune'], { start() {}, stop() {} }); }
	createMediaStreamDestination() {
		return this._create('msd', [], { stream: { getTracks: () => [], getAudioTracks: () => [] } });
	}
	addEventListener() {}
	removeEventListener() {}
	getOutputTimestamp() { return { contextTime: 0, performanceTime: 0 }; }
	async resume() { this.state = 'running'; }
	async suspend() { this.state = 'suspended'; }
	async close() {
		this.closed = true;
		this.state = 'closed';
	}
}

/** Everything upstream of `target` in one context's recorded edges. */
function ancestorsOf(ctx, target) {
	const seen = new Set();
	const stack = [target];
	while (stack.length > 0) {
		const current = stack.pop();
		for (const edge of ctx.edges) {
			if (edge.to === current && !seen.has(edge.from)) {
				seen.add(edge.from);
				stack.push(edge.from);
			}
		}
	}
	return seen;
}

function installWindow(search) {
	const store = new Map();
	globalThis.window = {
		location: { search, href: `http://127.0.0.1:9427/performance${search}` },
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		},
		addEventListener() {},
		removeEventListener() {},
		setTimeout,
		clearTimeout,
		setInterval,
		clearInterval
	};
}

let topology;
let audio;
let stores;
let status;

before(async () => {
	installWindow('');
	globalThis.AudioContext = FakeAudioContext;
	topology = await loadTypeScriptModule('src/lib/rb/audio-output-topology.ts');
	// One bundle, so the engine, the toast store and the status module share
	// module state exactly as they do in the page.
	const entry = await loadTypeScriptModule('tests/unit/fixtures/djio-fallback-entry.ts');
	audio = entry.audio;
	stores = entry.stores;
	status = entry.status;
});

beforeEach(async () => {
	await audio.engine.dispose();
	FakeAudioContext.maxChannelCount = 2;
	FakeAudioContext.failWhen = null;
	FakeAudioContext.instances = [];
	clearToasts();
});

/** Dismiss through the store so each toast's auto-dismiss timer is cleared too. */
function clearToasts() {
	for (const toast of [...stores.toasts]) stores.dismissToast(toast.logId);
}

after(async () => {
	await audio.engine.dispose();
	clearToasts();
	delete globalThis.AudioContext;
	delete globalThis.window;
});

function wiredDecks() {
	return [1, 2, 3, 4].filter((deck) => audio.peekDeckFaderGain(deck) !== null);
}

//-----------------------------------------------------------------------------
// the <4-channel path
//-----------------------------------------------------------------------------

describe('IOPIN-12: djio degrades to stereo master on an output with fewer than 4 channels', () => {
	test('IOPIN-12: resolveDjOutputProfile falls back below 4 channels and keeps djio at 4 or more', () => {
		const fallback = topology.resolveDjOutputProfile(DJIO, 2);
		assert.equal(fallback.requested, DJIO);
		assert.equal(fallback.profile, null, 'if a 2-channel output keeps the djio profile then the 4-channel wiring throws - broken');
		assert.equal(fallback.fallback.available_channels, 2);
		assert.match(fallback.fallback.message, /Mixtour Pro as the macOS output device and reload/);
		const full = topology.resolveDjOutputProfile(DJIO, 4);
		assert.equal(full.profile, DJIO, 'if a 4-channel output falls back too then cue 3/4 never works - broken');
		assert.equal(full.fallback, null);
		const none = topology.resolveDjOutputProfile(null, 2);
		assert.deepEqual(none, { requested: null, profile: null, fallback: null }, 'no djio request is not a fallback');
	});

	test('IOPIN-12: a 2-channel output builds a stereo graph with every deck wired and names the fix', async () => {
		installWindow(`?djio=${DJIO}`);
		FakeAudioContext.maxChannelCount = 2;
		await audio.ensureAudioGraphForCue();
		const ctx = FakeAudioContext.instances.at(-1);
		assert.deepEqual(wiredDecks(), [1, 2, 3, 4], 'if any deck is unwired then its next load fails with "audio graph is missing" - broken');
		assert.equal(ctx.destination.channelCount, 2, 'if the destination is forced to 4 channels on a 2-channel device then output is undefined - broken');
		assert.equal(
			ctx.edges.some((edge) => edge.to.kind === 'merger' && edge.input === 3),
			false,
			'if a cue 3/4 merger is wired on a 2-channel output then the monitor path targets channels that do not exist - broken'
		);
		assert.ok(
			ancestorsOf(ctx, ctx.destination).has(audio.masterDelayNode()),
			'the stereo master still reaches the destination through the room delay'
		);
		assert.equal(status.audioOutputStatus.requested_profile, DJIO);
		assert.equal(status.audioOutputStatus.active_profile, null);
		assert.equal(status.audioOutputStatus.fallback?.available_channels, 2);
		const warn = stores.toasts.find((toast) => toast.kind === 'warn' && /Mixtour/.test(toast.message));
		assert.ok(warn, 'if the fallback raises no toast then the operator never learns why cue 3/4 is silent - broken');
		assert.match(warn.message, /Mixtour Pro as the macOS output device and reload/);
		assert.deepEqual(status.outputTopologyMirror(), {
			requested_profile: DJIO,
			active_profile: null,
			fallback: status.audioOutputStatus.fallback
		}, 'agents read the same fallback the I/O panel shows');
	});

	test('IOPIN-12 control: a 4-channel output still wires master 1/2 and cue 3/4', async () => {
		installWindow(`?djio=${DJIO}`);
		FakeAudioContext.maxChannelCount = 4;
		await audio.ensureAudioGraphForCue();
		const ctx = FakeAudioContext.instances.at(-1);
		assert.deepEqual(wiredDecks(), [1, 2, 3, 4]);
		assert.equal(ctx.destination.channelCount, 4, 'if djio is not wired on a 4-channel output then the fallback overshot - broken');
		assert.ok(ctx.edges.some((edge) => edge.to.kind === 'merger' && edge.input === 3), 'cue reaches channel 4');
		assert.equal(status.audioOutputStatus.active_profile, DJIO);
		assert.equal(status.audioOutputStatus.fallback, null);
		assert.equal(stores.toasts.some((toast) => /Mixtour/.test(toast.message)), false, 'no fallback, no toast');
	});

	test('IOPIN-12: re-entering djio across rebuilds switches fallback -> djio -> fallback without throwing', async () => {
		installWindow(`?djio=${DJIO}`);
		for (const [channels, active] of [[2, null], [4, DJIO], [2, null]]) {
			await audio.engine.dispose();
			FakeAudioContext.maxChannelCount = channels;
			await audio.ensureAudioGraphForCue();
			assert.deepEqual(wiredDecks(), [1, 2, 3, 4], `${channels} channels: every deck wired`);
			assert.equal(status.audioOutputStatus.active_profile, active, `${channels} channels: active profile`);
			assert.equal(status.audioOutputStatus.fallback === null, active !== null);
		}
		await audio.engine.dispose();
		assert.deepEqual(status.outputTopologyMirror(), { requested_profile: null, active_profile: null, fallback: null },
			'if teardown leaves the fallback status behind then the I/O panel warns about a graph that no longer exists - broken');
	});
});

//-----------------------------------------------------------------------------
// the dead deck: any failed build must leave the deck recoverable
//-----------------------------------------------------------------------------

describe('IOPIN-12: a failed graph build never leaves a deck dead', () => {
	test('IOPIN-12: a throw mid-build is discarded and the next build wires every deck on a fresh context', async () => {
		installWindow('');
		// Fail on the 3rd biquad of the FIRST context: deck 1's channel nodes are
		// already published by then, which is the half-built state the live bug hit.
		FakeAudioContext.failWhen = (ctx, kind, nth) =>
			ctx === FakeAudioContext.instances[0] && kind === 'biquad' && nth === 3;
		await assert.rejects(audio.ensureAudioGraphForCue(), /injected biquad construction failure/);
		const failed = FakeAudioContext.instances[0];
		assert.equal(audio.gigDeckGraphIsPresent(), false, 'if the half-built context stays installed then _ensureGraph() keeps returning it - broken');
		assert.deepEqual(wiredDecks(), [], 'no deck may keep nodes that belong to the discarded context');
		await new Promise((resolve) => setImmediate(resolve));
		assert.equal(failed.closed, true, 'the discarded context is closed, not leaked');

		await audio.ensureAudioGraphForCue();
		assert.equal(FakeAudioContext.instances.length, 2, 'the retry builds a NEW context');
		assert.deepEqual(
			wiredDecks(),
			[1, 2, 3, 4],
			'if a deck is unwired after the retry then every load fails with "load: deck N audio graph is missing" - broken'
		);
	});

	test('IOPIN-12: the live sequence (throw on the first load, retry on the same deck) leaves deck 2 loadable', async () => {
		// The Fri 25 Sep order: the first build throws BEFORE any deck node exists
		// (there it was the 4-channel check), after the master bus and the headphone
		// monitor graph were already built on the failed context. The retry must not
		// reuse that monitor graph: wiring it into a new context throws on its own.
		installWindow('');
		FakeAudioContext.failWhen = (ctx, kind, nth) =>
			ctx === FakeAudioContext.instances[0] && kind === 'biquad' && nth === 1;
		await assert.rejects(audio.ensureAudioGraphForCue(), /injected biquad/);
		await audio.ensureAudioGraphForCue();
		const retry = FakeAudioContext.instances.at(-1);
		assert.notEqual(audio.peekDeckFaderGain(2), null, 'deck 2 has a channel graph to load into');
		assert.ok(retry.edges.every((edge) => edge.to.kind === undefined || edge.to.context === retry), 'nothing from the failed context is wired into the retry');
	});
});

//-----------------------------------------------------------------------------
// re-entry: the controller redirect
//-----------------------------------------------------------------------------

describe('IOPIN-12: the controller redirect into djio fires once and never loops', () => {
	const base = 'http://127.0.0.1:9427/performance';

	test('IOPIN-12: a page without djio is redirected once, keeping its other params and hash', () => {
		const target = topology.djioRedirectTarget(`${base}?muted=1#deck2`, DJIO);
		assert.equal(target, `${base}?muted=1&djio=${DJIO}#deck2`);
		assert.equal(
			topology.djioRedirectTarget(target, DJIO),
			null,
			'if the redirected URL redirects again then location.replace loops - broken'
		);
	});

	test('IOPIN-12: an existing djio (fallback page included), an explicit empty djio, or extroute is never redirected', () => {
		assert.equal(topology.djioRedirectTarget(`${base}?djio=${DJIO}`, DJIO), null, 'a stereo-fallback page keeps its param, so it must not re-redirect');
		assert.equal(topology.djioRedirectTarget(`${base}?djio=`, DJIO), null, 'an explicit empty djio is the operator opting out');
		assert.equal(
			topology.djioRedirectTarget(`${base}?extroute=1:1,2:3`, DJIO),
			null,
			'if extroute gains djio then the two exclusive topologies throw on every graph build - broken'
		);
	});

});

//-----------------------------------------------------------------------------
// re-entry: the installed shell's MIDI boot, driven for real
//-----------------------------------------------------------------------------

describe('IOPIN-12: the native-shell MIDI boot redirects once and never subscribes on a page that is leaving', () => {
	// Page boot runs maybeAutoEnableMidi() twice more often than not (TopBar
	// onMount, then the prefs hydration), so initMidi() runs twice before the
	// redirect's navigation lands. A Tauri listener registered on the leaving
	// page stays alive after reload and doubles every physical message.
	let webmidi;
	let shell;
	// Every interval the module starts is cleared after each case. The reset
	// hook clears the one handle it holds, but a double subscribe overwrites
	// that handle, and the leaked 1 s hot-plug poll would then keep this file's
	// process alive forever: a regression here must fail, not hang the suite.
	const realSetInterval = globalThis.setInterval;
	const shellIntervals = [];

	before(async () => {
		webmidi = await loadTypeScriptModule('src/lib/rb/midi/webmidi.svelte.ts');
		const { RELOOP_MIXTOUR_PRO_MAP } = await loadTypeScriptModule('src/lib/rb/midi/maps/reloop-mixtour-pro.ts');
		shell = { map: RELOOP_MIXTOUR_PRO_MAP };
	});

	/**
	 * The installed shell as the page sees it: Tauri's IPC entry point and a
	 * location whose replace() is recorded. href does not change on replace(),
	 * exactly as in a browser: the old document keeps its URL until it is gone.
	 * The snapshot is the Air's Mixtour Pro as CoreMIDI names it.
	 */
	function installShell(search) {
		installWindow(search);
		const record = { replaced: [], invoked: [] };
		window.location.replace = (url) => record.replaced.push(String(url));
		window.__TAURI_INTERNALS__ = {
			invoke: async (cmd) => {
				record.invoked.push(cmd);
				if (cmd === 'native_midi_snapshot') {
					return [{ id: 'mixtour-pro', name: 'Reloop Mixtour Pro', manufacturer: 'Reloop', hasOutput: true }];
				}
				if (cmd === 'plugin:event|listen') return 1;
				if (cmd === 'plugin:event|unlisten' || cmd === 'native_midi_send') return null;
				throw new Error(`unexpected shell command ${cmd}`);
			},
			transformCallback: () => 1
		};
		window.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
		globalThis.setInterval = (...args) => {
			const handle = realSetInterval(...args);
			shellIntervals.push(handle);
			return handle;
		};
		webmidi._resetMidiForTests();
		webmidi.registerDeviceMap(shell.map);
		return record;
	}

	const listens = (record) => record.invoked.filter((cmd) => cmd === 'plugin:event|listen').length;

	afterEach(() => {
		webmidi._resetMidiForTests();
		for (const handle of shellIntervals.splice(0)) clearInterval(handle);
		globalThis.setInterval = realSetInterval;
		installWindow('');
	});

	test('IOPIN-12: two sequential boots on a page without djio replace() once and subscribe nothing', async () => {
		const record = installShell('');
		await webmidi.initMidi();
		await webmidi.initMidi();
		assert.deepEqual(record.replaced, [`http://127.0.0.1:9427/performance?djio=${DJIO}`]);
		assert.equal(record.replaced.length, 1, 'if the one-shot guard resets then every boot or rescan re-issues location.replace - broken');
		assert.equal(listens(record), 0, 'if the second boot subscribes on the leaving page then every Mixtour press arrives twice after reload - broken');
	});

	test('IOPIN-12: two concurrent boots on a page without djio replace() once and subscribe nothing', async () => {
		const record = installShell('');
		await Promise.all([webmidi.initMidi(), webmidi.initMidi()]);
		assert.equal(record.replaced.length, 1);
		assert.equal(listens(record), 0, 'if the boot that loses the race subscribes then the leaving page keeps a live Tauri listener - broken');
	});

	test('IOPIN-12 control: a page already on djio never redirects and does subscribe', async () => {
		const record = installShell(`?djio=${DJIO}`);
		await webmidi.initMidi();
		await webmidi.initMidi();
		assert.deepEqual(record.replaced, [], 'if a djio page replaces itself then the reload loops forever - broken');
		assert.equal(listens(record), 1, 'if the fix stops subscribing altogether then the Mixtour is dead on its own page - broken');
	});
});

//-----------------------------------------------------------------------------
// the I/O panel and agent parity surfaces
//-----------------------------------------------------------------------------

describe('IOPIN-12: the fallback is visible in the I/O panel and to agents', () => {
	test('IOPIN-12: the I/O panel renders the fallback message inline with a hover title for its channel count', () => {
		const panel = readFileSync(`${SRC}/lib/components/rb/mixer/HeadphoneCluster.svelte`, 'utf8');
		assert.match(panel, /\{#if audioOutputStatus\.fallback !== null\}/);
		assert.match(panel, /data-djio-fallback-notice/);
		assert.match(panel, /\{audioOutputStatus\.fallback\.message\}/);
		assert.match(panel, /title=\{djioFallbackTitle\(audioOutputStatus\.fallback\)\}/);
	});

	test('IOPIN-12: the ui-mirror publishes output_topology for agents', () => {
		const mirror = readFileSync(`${SRC}/lib/rb/ui-mirror.ts`, 'utf8');
		assert.match(mirror, /output_topology: outputTopologyMirror\(\)/);
	});
});
