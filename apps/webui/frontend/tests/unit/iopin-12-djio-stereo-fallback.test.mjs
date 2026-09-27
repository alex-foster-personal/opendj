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
// [if] graph construction throws once, before or after a deck is published
//   [then] no deck keeps nodes of the discarded context, the context is closed,
//   unregistered and disarmed, and the next build wires every deck on a fresh
//   context [⛔️ if the half-built context or any of its deck nodes is reused]
// [if] that throw happens inside a headphone or master output selection [then]
//   the selection rejects with the build's own error and the I/O panel error is
//   set [⛔️ if it reports "stale headphone operation" instead]
// [if] the fallback graph is rebuilt while its toast is on screen [then] there
//   is still one toast, counted [⛔️ if each rebuild stacks another]
// [if] the page is already on ?djio= (including a stereo-fallback page) or on
//   ?extroute= [then] the controller redirect does not fire again [⛔️ if
//   location.replace can loop or produce a URL whose graph cannot build]
// [if] the shell's MIDI boot runs twice before the redirect lands [then] it
//   replaces once and subscribes nothing on the leaving page [⛔️ if a Tauri
//   listener survives into the reload and doubles every press]
// [if] a page that already subscribed redirects (Mixtour hot-plugged, or an
//   in-app link dropped djio) [then] its poll stops and the unlisten IPC lands
//   before its one replace() [⛔️ if replace() races a live listener]
// [if] a fallback is live [then] the rendered I/O panel and the agent ui-mirror
//   both carry it [⛔️ if either surface omits it]
import assert from 'node:assert/strict';
import { after, afterEach, before, beforeEach, describe, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

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
		/** Every node made through `_create`, in creation order. */
		this.created = [];
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
		const node = fakeNode(this, kind, params, extra);
		this.created.push(node);
		return node;
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
let player;
let registry;
let mirror;

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
	player = entry.player;
	registry = entry.registry;
	mirror = entry.mirror;
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

	test('IOPIN-12: repeated fallback builds keep ONE warn toast on screen, counted, and re-raise it once it has expired', async () => {
		installWindow(`?djio=${DJIO}`);
		const fallbackToasts = () => stores.toasts.filter((toast) => toast.kind === 'warn' && /Mixtour 4-channel output unavailable/.test(toast.message));
		for (let build = 1; build <= 2; build += 1) {
			await audio.engine.dispose();
			await audio.ensureAudioGraphForCue();
		}
		assert.equal(fallbackToasts().length, 1, 'if every graph rebuild stacks its own toast then a watchdog rebuild loop buries the tray - broken');
		assert.equal(fallbackToasts()[0].count, 2, 'the repeat is counted on the one toast, not dropped');
		// Expiry dismisses the toast, and the next build names the fix again: the
		// inline I/O notice is the persistent surface, the toast is on-screen only.
		stores.dismissToast(fallbackToasts()[0].logId);
		await audio.engine.dispose();
		await audio.ensureAudioGraphForCue();
		assert.equal(fallbackToasts().length, 1);
		assert.equal(fallbackToasts()[0].count, 1);
	});
});

//-----------------------------------------------------------------------------
// the dead deck: any failed build must leave the deck recoverable
//-----------------------------------------------------------------------------

describe('IOPIN-12: a failed graph build never leaves a deck dead', () => {
	test('IOPIN-12: a throw after deck 1 is published is discarded and the next build wires every deck on a fresh context', async () => {
		installWindow('');
		// buildDeckChannelGraph makes 5 biquads per deck (low, mid, high, filterLp,
		// filterHp) BEFORE it publishes that deck, so biquad 6 is deck 2's first:
		// deck 1 already holds nodes of this context when the build throws. That
		// is the partial-construction case; a throw before ANY deck is published
		// is the live-sequence test below.
		FakeAudioContext.failWhen = (ctx, kind, nth) =>
			ctx === FakeAudioContext.instances[0] && kind === 'biquad' && nth === 6;
		await assert.rejects(audio.ensureAudioGraphForCue(), /injected biquad construction failure/);
		const failed = FakeAudioContext.instances[0];
		assert.equal(failed.calls.get('biquad'), 6, 'precondition: the build got past deck 1 and failed inside deck 2');
		assert.equal(audio.gigDeckGraphIsPresent(), false, 'if the half-built context stays installed then _ensureGraph() keeps returning it - broken');
		assert.deepEqual(wiredDecks(), [], 'if deck 1 keeps nodes of the discarded context then its trim/fader writes hit a dead graph - broken');
		assert.doesNotThrow(
			() => audio.engine.setFader(1, 0.5),
			'if deck 1 keeps stale nodes while no graph exists then every fader move throws "audio graph not initialised" - broken'
		);
		const deckOneEq = failed.created.filter((node) => node.kind === 'biquad').slice(0, 5);
		assert.equal(deckOneEq.length, 5);
		assert.ok(
			deckOneEq.every((node) => failed.disconnected.has(node)),
			'if the published deck-1 nodes are left out of the teardown list then they stay wired on the discarded context - broken'
		);
		assert.equal(
			registry.countRegisteredAudioContexts(),
			0,
			'if the discarded context stays registered then library mode reports a leaked context (PERFMODE-14) - broken'
		);
		assert.equal(
			window.__mdtAudioOutput,
			undefined,
			'if the watchdog armed on the discarded context is not disarmed then agents read the health of a dead context - broken'
		);
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
// a failed build inside an output selection reports ITS cause
//-----------------------------------------------------------------------------

describe('IOPIN-12: a graph build that fails inside an output selection reports its real cause', () => {
	const savedNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
	// The selections build the graph lazily through the engine's monitor source,
	// inside their own try block. Both triggers below are real URL-borne ones.
	const cases = [
		{
			name: 'selectHeadphoneOutput with ?djio=bogus',
			select: (id) => audio.engine.selectHeadphoneOutput(id),
			search: '?djio=bogus',
			channels: 2,
			expected: /^headphone output selection failed: djio: unsupported profile 'bogus'/
		},
		{
			name: 'selectMasterOutput with ?extroute=1:3 on a 2-channel output',
			select: (id) => audio.engine.selectMasterOutput(id),
			search: '?extroute=1:3',
			channels: 2,
			expected: /^master output selection failed: extroute needs 4 output channels but the current output device exposes 2/
		}
	];

	beforeEach(() => {
		// A device-selection API double, as the browser presents it: the build
		// throws before any sink call is made, so no sink method is reached.
		const mediaDevices = { enumerateDevices: async () => [], addEventListener() {}, removeEventListener() {} };
		Object.defineProperty(globalThis, 'navigator', { value: { mediaDevices }, configurable: true, writable: true });
		player.mixerState.headphones.outputs = [{ id: 'dev-a', label: 'dev-a' }];
	});

	afterEach(() => {
		if (savedNavigator === undefined) delete globalThis.navigator;
		else Object.defineProperty(globalThis, 'navigator', savedNavigator);
		const hp = player.mixerState.headphones;
		hp.outputs = [];
		hp.error = null;
		hp.selected_output_device_id = null;
		hp.selected_master_output_device_id = null;
		hp.routes = { master: { state: 'default', selected: false }, cue: { state: 'default', selected: false } };
	});

	for (const { name, select, search, channels, expected } of cases) {
		test(`IOPIN-12: ${name} rejects with the build's own error and sets the I/O panel error`, async () => {
			installWindow(search);
			FakeAudioContext.maxChannelCount = channels;
			await assert.rejects(
				select('dev-a'),
				(error) => {
					assert.match(
						error.message,
						expected,
						'if the discard retires the selection it ran inside then it reports "stale headphone operation" and drops the real cause - broken'
					);
					return true;
				}
			);
			assert.match(
				player.mixerState.headphones.error ?? '',
				expected,
				'if headphones.error stays null then the I/O panel shows no reason for the failed selection - broken'
			);
			assert.equal(audio.gigDeckGraphIsPresent(), false, 'the failed build is still discarded');
			installWindow('');
			await audio.ensureAudioGraphForCue();
			assert.deepEqual(wiredDecks(), [1, 2, 3, 4], 'and the next build still wires every deck');
		});
	}

	test('IOPIN-12 control: route teardown still retires a selection in flight, so it cannot publish onto the next route', async () => {
		// The overshoot of the fix above: a failed build must not retire the
		// selection it runs in, but teardown still must, or a sink call that
		// lands after the route is gone publishes onto whatever mounts next.
		installWindow('');
		let landSink = null;
		FakeAudioContext.prototype.setSinkId = () => new Promise((resolve) => { landSink = resolve; });
		try {
			const pending = audio.engine.selectMasterOutput('dev-a');
			assert.equal(typeof landSink, 'function', 'precondition: the selection is parked on setSinkId');
			await audio.engine.dispose();
			landSink();
			await assert.rejects(
				pending,
				/stale headphone operation/,
				'if teardown stops retiring in-flight selections then a late sink publishes onto a disposed route - broken'
			);
			assert.equal(player.mixerState.headphones.selected_master_output_device_id, null, 'the retired selection published nothing');
		} finally {
			delete FakeAudioContext.prototype.setSinkId;
		}
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

	const realClearInterval = globalThis.clearInterval;
	const MIXTOUR = { id: 'mixtour-pro', name: 'Reloop Mixtour Pro', manufacturer: 'Reloop', hasOutput: true };

	/**
	 * The installed shell as the page sees it: Tauri's IPC entry point and a
	 * location whose replace() is recorded. href does not change on replace(),
	 * exactly as in a browser: the old document keeps its URL until it is gone.
	 * The snapshot is the Air's Mixtour Pro as CoreMIDI names it; `devices`
	 * lets a case plug it in after boot. `timeline` orders IPC calls against
	 * replace(); `holdUnlisten` parks the unlisten IPC until `releaseUnlisten()`.
	 */
	function installShell(search, { devices = [MIXTOUR], holdUnlisten = false } = {}) {
		installWindow(search);
		const record = { replaced: [], invoked: [], timeline: [], devices, polls: [], cleared: new Set(), releaseUnlisten: null };
		window.location.replace = (url) => {
			record.replaced.push(String(url));
			record.timeline.push('replace');
		};
		window.__TAURI_INTERNALS__ = {
			invoke: async (cmd) => {
				record.invoked.push(cmd);
				record.timeline.push(cmd);
				if (cmd === 'native_midi_snapshot') return record.devices;
				if (cmd === 'plugin:event|listen') return 1;
				if (cmd === 'plugin:event|unlisten' && holdUnlisten) {
					await new Promise((resolve) => { record.releaseUnlisten = resolve; });
					record.timeline.push('unlisten-landed');
					return null;
				}
				if (cmd === 'plugin:event|unlisten' || cmd === 'native_midi_send') return null;
				throw new Error(`unexpected shell command ${cmd}`);
			},
			transformCallback: () => 1
		};
		window.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
		globalThis.setInterval = (fn, ms, ...rest) => {
			const handle = realSetInterval(fn, ms, ...rest);
			shellIntervals.push(handle);
			// The 1 s hot-plug poll, captured so a case can run one tick without waiting a second.
			if (ms === 1000) record.polls.push({ tick: fn, handle });
			return handle;
		};
		globalThis.clearInterval = (handle) => {
			record.cleared.add(handle);
			realClearInterval(handle);
		};
		webmidi._resetMidiForTests();
		webmidi.registerDeviceMap(shell.map);
		return record;
	}

	const listens = (record) => record.invoked.filter((cmd) => cmd === 'plugin:event|listen').length;
	const unlistens = (record) => record.invoked.filter((cmd) => cmd === 'plugin:event|unlisten').length;
	/** Let every promise chain a poll tick started run to completion. */
	const settle = async () => {
		for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
	};

	afterEach(() => {
		webmidi._resetMidiForTests();
		for (const handle of shellIntervals.splice(0)) realClearInterval(handle);
		globalThis.setInterval = realSetInterval;
		globalThis.clearInterval = realClearInterval;
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
		// The overshoot of the teardown below: a poll tick on the page the redirect
		// lands on must keep its listener and its poll.
		assert.equal(record.polls.length, 1, 'precondition: the subscribed page runs the hot-plug poll');
		record.polls[0].tick();
		await settle();
		assert.equal(unlistens(record), 0, 'if a djio page drops its listener on a poll tick then the Mixtour goes dead on its own page - broken');
		assert.equal(record.cleared.has(record.polls[0].handle), false, 'if a djio page stops its poll then an unplug is never noticed - broken');
	});

	test('IOPIN-12: a subscribed page whose snapshot gains the Mixtour unsubscribes and stops polling before its one replace()', async () => {
		// Hot-plug: the page booted with no Mixtour, so it subscribed and started
		// the poll. The Mixtour is then plugged in and the next tick redirects.
		const record = installShell('', { devices: [], holdUnlisten: true });
		await webmidi.initMidi();
		assert.equal(listens(record), 1, 'precondition: the page booted without a Mixtour subscribes');
		assert.equal(record.polls.length, 1, 'precondition: and runs the hot-plug poll');
		record.devices = [MIXTOUR];
		record.polls[0].tick();
		await settle();
		assert.equal(unlistens(record), 1, 'if the leaving page keeps its Tauri listener then every Mixtour press arrives twice after the reload - broken');
		assert.deepEqual(
			record.replaced,
			[],
			'if replace() fires while the unlisten IPC is still in flight then the navigation can cancel it and the listener survives the reload - broken'
		);
		assert.ok(record.cleared.has(record.polls[0].handle), 'if the poll keeps running then it rescans a page that is already leaving - broken');
		record.releaseUnlisten();
		await settle();
		assert.deepEqual(record.replaced, [`http://127.0.0.1:9427/performance?djio=${DJIO}`]);
		assert.deepEqual(
			record.timeline.slice(record.timeline.indexOf('plugin:event|unlisten')),
			['plugin:event|unlisten', 'unlisten-landed', 'replace'],
			'replace() comes after the unlisten IPC has landed, never before'
		);
		record.polls[0].tick();
		await settle();
		assert.equal(record.replaced.length, 1, 'a straggling tick after the redirect does not replace() again');
	});

	test('IOPIN-12: an in-app navigation that drops djio re-enters it with one replace(), after releasing native MIDI', async () => {
		// SvelteKit client-side links are plain paths (routes/+layout.svelte), so
		// leaving /performance?djio=... for / drops the param while this module,
		// its listener and its poll live on. The poll then sees the map again.
		const record = installShell(`?djio=${DJIO}`);
		await webmidi.initMidi();
		assert.equal(listens(record), 1, 'precondition: the djio page subscribes');
		window.location.href = 'http://127.0.0.1:9427/';
		window.location.search = '';
		record.polls[0].tick();
		await settle();
		assert.deepEqual(record.replaced, [`http://127.0.0.1:9427/?djio=${DJIO}`]);
		assert.equal(unlistens(record), 1, 'if the in-app re-entry reloads with the listener live then presses double after it - broken');
		assert.ok(record.cleared.has(record.polls[0].handle), 'the poll stops before the re-entry reload');
		assert.ok(record.timeline.indexOf('plugin:event|unlisten') < record.timeline.indexOf('replace'));
	});
});

//-----------------------------------------------------------------------------
// the I/O panel and agent parity surfaces
//-----------------------------------------------------------------------------

describe('IOPIN-12: the fallback is visible in the I/O panel and to agents', () => {
	// The real HeadphoneCluster, compiled for the server and rendered by
	// svelte's own renderer (see load-svelte-ssr.mjs for what that can and
	// cannot prove). Its own bundle, so it reads its own status store.
	const PANEL_ENTRY = [
		"export { default as Panel } from '$lib/components/rb/mixer/HeadphoneCluster.svelte';",
		"export { ioSurface } from '$lib/rb/io-surface.svelte';",
		"export { audioOutputStatus, clearDjOutputResolution } from '$lib/rb/audio-output-status.svelte';",
		"export { resolveDjOutputProfile } from '$lib/rb/audio-output-topology';",
		"export { mixerState } from '$lib/player/state.svelte';",
		"export { render } from 'svelte/server';"
	].join('\n');
	let panel;

	before(async () => {
		panel = await loadSvelteSsrModule(PANEL_ENTRY);
	});

	/** Render the open I/O panel for one fallback (or none); return its markup. */
	function renderIoPanel(fallback) {
		panel.clearDjOutputResolution();
		if (fallback !== null) panel.audioOutputStatus.fallback = fallback;
		panel.ioSurface.open = true;
		const noop = () => {};
		const props = Object.fromEntries(
			['onmix', 'onlevel', 'ondelay', 'onrefresh', 'onacquire', 'onselect', 'onmaster', 'oninput', 'onmode', 'oncalibrate', 'onAlignmentMode'].map((name) => [name, noop])
		);
		try {
			return panel.render(panel.Panel, { props: { state: panel.mixerState.headphones, ...props } }).body;
		} finally {
			panel.ioSurface.open = false;
			panel.clearDjOutputResolution();
		}
	}

	test('IOPIN-12: the open I/O panel renders the fallback inline, with a hover title for its channel count', () => {
		const html = renderIoPanel(panel.resolveDjOutputProfile(DJIO, 2).fallback);
		const notice = html.match(/<p\b[^>]*\bdata-djio-fallback-notice\b[^>]*>([^<]*)<\/p>/);
		assert.ok(notice, `if the notice does not render then the I/O panel never says why cue 3/4 is silent - broken; rendered: ${html.slice(0, 400)}`);
		assert.match(notice[1], /Select the Mixtour Pro as the macOS output device and reload/);
		assert.match(notice[0], /role="status"/);
		assert.match(notice[0], /title="2 = output channels the current macOS output device exposes/, 'numeric readouts carry a hover title');
	});

	test('IOPIN-12 control: with no fallback the open I/O panel renders no notice at all', () => {
		const html = renderIoPanel(null);
		assert.match(html, /hp-context/, 'precondition: the open I/O section did render');
		assert.equal(html.includes('data-djio-fallback-notice'), false, 'if the notice renders without a fallback then a healthy 4-channel set carries a false warning - broken');
	});

	test('IOPIN-12: the ui-mirror an agent reads carries the same fallback after a 2-channel build', async () => {
		installWindow(`?djio=${DJIO}`);
		FakeAudioContext.maxChannelCount = 2;
		await audio.ensureAudioGraphForCue();
		// buildUiMirror also lists controls and overlays from the page; an empty
		// document stands in for them, scoped to this one call because the
		// engine arms document listeners whenever a document exists.
		globalThis.document = { querySelectorAll: () => [] };
		let published;
		try {
			published = mirror.buildUiMirror();
		} finally {
			delete globalThis.document;
		}
		assert.equal(
			published.output_topology?.fallback?.available_channels,
			2,
			'if the mirror omits output_topology then an agent cannot see the stereo fallback the operator sees - broken'
		);
		assert.equal(published.output_topology.requested_profile, DJIO);
		assert.equal(published.output_topology.active_profile, null);
		assert.match(published.output_topology.fallback.message, /Select the Mixtour Pro as the macOS output device and reload/);
	});
});
