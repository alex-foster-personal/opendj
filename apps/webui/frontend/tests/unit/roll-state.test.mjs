// requirement: FXROLL-01 (issue #9840)
// [if] roll engage/release on a playing deck [then] slip forces on and returns to the hidden head, [else stop].
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://roll-state.example.test';
const SID = 'c'.repeat(40);
const STUB_AUDIO_BYTES = 48_000 * 4 * 20;
const STRETCH_STUB = fileURLToPath(
	new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url)
);

globalThis.location ??= { search: '' };

function _beats(n) {
	const out = [];
	for (let i = 0; i < n; i++) out.push({ n: (i % 4) + 1, bpm: 128, t: i * 0.469 });
	return out;
}

let roll;
let ipc;
let rust;

before(async () => {
	[roll, ipc, rust] = await Promise.all([
		loadTypeScriptModule('src/lib/player/transport/roll-state.ts'),
		loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts'),
		loadTypeScriptModule('src/lib/audio-engine/rust-mode.svelte.ts')
	]);
});

// ------------------------------------------------------------- roll-state pure

test('assertRollBeats accepts each power-of-two length and refuses outliers', () => {
	for (const beats of roll.ROLL_BEATS) {
		assert.equal(roll.assertRollBeats(beats), beats);
	}
	for (const bad of [0.3, 3, 64, 0, -1, Number.NaN, '1']) {
		assert.throws(
			() => roll.assertRollBeats(bad),
			(error) => {
				assert.match(String(error), /1\/32, 1\/16, 1\/8, 1\/4, 1\/2, 1, 2, 4, 8, 16, 32/);
				return true;
			}
		);
	}
});

test('roll stack push and release resume then end', () => {
	const saved = { slipEnabled: false, slipActive: false, loop: null };
	let state = roll.beginRoll(saved, { beats: 1, in_ms: 100, out_ms: 200 });
	state = roll.pushRoll(state, { beats: 0.25, in_ms: 150, out_ms: 162 });
	assert.deepEqual(roll.rollQueryFields(state), { roll_beats: 0.25, roll_stack: [1, 0.25] });
	const resume = roll.releaseRoll(state);
	assert.equal(resume.kind, 'resume');
	assert.equal(resume.entry.beats, 1);
	assert.deepEqual(roll.rollQueryFields(resume.state), { roll_beats: 1, roll_stack: [1] });
	const end = roll.releaseRoll(resume.state);
	assert.equal(end.kind, 'end');
	assert.deepEqual(end.restore, saved);
});

test('releaseRoll end restore carries slip_enabled true and a saved regular loop', () => {
	const savedLoop = { engaged: true, beat_length: 4, in_ms: 100, out_ms: 400 };
	const saved = { slipEnabled: true, slipActive: true, loop: savedLoop };
	let state = roll.beginRoll(saved, { beats: 0.25, in_ms: 150, out_ms: 162 });
	const end = roll.releaseRoll(state);
	assert.equal(end.kind, 'end');
	assert.equal(end.restore.slipEnabled, true);
	assert.deepEqual(end.restore.loop, savedLoop);
});

test('releaseRoll with no held roll is a no-op', () => {
	assert.equal(roll.releaseRoll(null), null);
	assert.deepEqual(roll.rollQueryFields(null), { roll_beats: null, roll_stack: [] });
});

test('pushRoll refuses past eight and does not mutate frozen inputs', () => {
	const saved = Object.freeze({ slipEnabled: true, slipActive: false, loop: null });
	let state = roll.beginRoll(saved, { beats: 1, in_ms: 0, out_ms: 10 });
	for (let i = 0; i < roll.ROLL_STACK_MAX - 1; i++) {
		state = roll.pushRoll(state, { beats: 0.25, in_ms: 0, out_ms: 5 });
	}
	assert.throws(
		() => roll.pushRoll(state, { beats: 0.25, in_ms: 0, out_ms: 5 }),
		/full \(8 held rolls\)/
	);
});

// ------------------------------------------------------------- dispatcher

test('roll command parses and scopes like slip', () => {
	assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'roll', deck: 1, beats: 0.25 }), {
		type: 'roll',
		deck: 1,
		beats: 0.25
	});
	assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'roll', deck: 1, beats: null }), {
		type: 'roll',
		deck: 1,
		beats: null
	});
	assert.throws(
		() => ipc.parsePerformanceCommandForTest({ type: 'roll', deck: 1, beats: 3 }),
		/1\/32/
	);
	assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'roll', deck: 1 }), /required/);
	assert.throws(
		() => ipc.parsePerformanceCommandForTest({ type: 'roll', deck: 1, beats: 1, extra: true }),
		/unexpected fields/
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'roll', deck: 2, beats: 1 }),
		ipc.performanceCommandQueueScopes({ type: 'slip', deck: 2, enabled: true })
	);
});

test('rust engine mode refuses roll by name', () => {
	assert.ok(rust.WEB_AUDIO_ONLY.has('roll'));
	assert.equal(rust.ENGINE_COMMANDS.has('roll'), false);
});

// ------------------------------------------------------------- engine (playing-deck harness)

let transition;
let originalFetch;

function makeAudioParam(value = 1) {
	return {
		value,
		setValueAtTime(v) {
			this.value = v;
		},
		setTargetAtTime(v) {
			this.value = v;
		},
		linearRampToValueAtTime() {}
	};
}

function installBrowserAudioGlobals() {
	const localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
	Object.defineProperty(globalThis, 'window', {
		value: {
			location: { href: `${API_BASE}/performance`, search: '' },
			localStorage,
			addEventListener() {},
			removeEventListener() {}
		},
		configurable: true,
		writable: true
	});
	Object.defineProperty(globalThis, 'navigator', {
		value: { userAgent: 'roll-state-test' },
		configurable: true,
		writable: true
	});
	Object.defineProperty(globalThis, 'localStorage', {
		value: localStorage,
		configurable: true,
		writable: true
	});
	Object.defineProperty(globalThis, 'AudioWorkletNode', {
		value: function AudioWorkletNode() {
			return {
				connect() {},
				disconnect() {},
				port: { onmessage: null },
				addEventListener() {},
				removeEventListener() {}
			};
		},
		configurable: true,
		writable: true
	});
	Object.defineProperty(globalThis, 'requestAnimationFrame', {
		value: (cb) => setTimeout(() => cb(performance.now()), 0),
		configurable: true,
		writable: true
	});
	Object.defineProperty(globalThis, 'cancelAnimationFrame', {
		value: (id) => clearTimeout(id),
		configurable: true,
		writable: true
	});
	Object.defineProperty(globalThis, 'AudioContext', {
		value: class AudioContext {
			constructor() {
				this.state = 'running';
				this.sampleRate = 48_000;
				this.currentTime = 1;
				this.baseLatency = 128 / 48_000;
				this.outputLatency = 0.032;
				this.destination = { connect() {}, disconnect() {} };
				this.audioWorklet = { addModule: async () => {} };
			}
			#node(extra = {}) {
				return {
					context: this,
					connect() {},
					disconnect() {},
					gain: makeAudioParam(),
					frequency: makeAudioParam(1000),
					Q: makeAudioParam(1),
					...extra
				};
			}
			createGain() {
				return this.#node();
			}
			createBufferSource() {
				return this.#node({
					buffer: null,
					playbackRate: makeAudioParam(1),
					start() {},
					stop() {},
					onended: null
				});
			}
			createAnalyser() {
				return this.#node();
			}
			createBiquadFilter() {
				return this.#node({ type: 'lowpass' });
			}
			createChannelSplitter() {
				return this.#node();
			}
			createChannelMerger() {
				return this.#node();
			}
			createDelay() {
				return this.#node({ delayTime: makeAudioParam(0) });
			}
			createMediaStreamDestination() {
				return { context: this, stream: { getTracks: () => [] }, connect() {}, disconnect() {} };
			}
			createBuffer(_c, length, sampleRate) {
				return {
					duration: length / sampleRate,
					length,
					sampleRate,
					getChannelData: () => new Float32Array(length)
				};
			}
			async decodeAudioData(bytes) {
				const byteLength = bytes.byteLength ?? bytes.length ?? 0;
				const sampleRate = 48_000;
				const length = Math.max(1, Math.floor(byteLength / 4));
				return {
					duration: length / sampleRate,
					length,
					numberOfChannels: 2,
					sampleRate,
					getChannelData: () => new Float32Array(length)
				};
			}
			getOutputTimestamp() {
				return { contextTime: 1, performanceTime: performance.now() };
			}
			addEventListener() {}
			removeEventListener() {}
			async resume() {}
			async close() {}
		},
		configurable: true,
		writable: true
	});
}

function stubFetch() {
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		if (url.includes('/anlz?')) {
			const beats = _beats(32);
			return new Response(
				JSON.stringify({
					stable_id: SID,
					points: 38_400,
					waveform: {
						kind: 'tri',
						preview: { length: 1, low: [0], mid: [0], high: [0] },
						detail: { length: 3200, low: [0.4], mid: [0.3], high: [0.2] }
					},
					beatgrid: { source: 'own', status: 'ok', beat_count: beats.length, beats },
					beatgrid_source: 'own',
					cues: [],
					phrases: [],
					vocals: { status: 'not_analyzed' }
				}),
				{ headers: { 'content-type': 'application/json' } }
			);
		}
		if (url.endsWith('/hot-cues')) {
			return new Response(JSON.stringify([]), { headers: { 'content-type': 'application/json' } });
		}
		if (url.endsWith('/audio')) {
			return new Response(new Uint8Array(STUB_AUDIO_BYTES));
		}
		if (url.endsWith('/stems')) {
			return new Response(JSON.stringify({ detail: { code: 'STEMS_NOT_FOUND' } }), { status: 404 });
		}
		if (url.includes(`/tracks/${SID}`)) {
			return new Response(
				JSON.stringify({ stable_id: SID, title: 'Fixture', artist: 'Fixture', bpm: 128 }),
				{ headers: { 'content-type': 'application/json' } }
			);
		}
		throw new Error(`unexpected request ${url}`);
	};
}

before(async () => {
	originalFetch = globalThis.fetch;
	transition = await loadTypeScriptModule(
		'tests/unit/fixtures/auto-play-next-transition-entry.ts',
		{
			viteApiBase: API_BASE,
			alias: { '$lib/rb/stretch-adapter': STRETCH_STUB }
		}
	);
});

afterEach(async () => {
	if (originalFetch !== undefined) globalThis.fetch = originalFetch;
	installBrowserAudioGlobals();
	await transition.engine.dispose();
});

test('roll refuses paused or unloaded decks before mutating state', async () => {
	installBrowserAudioGlobals();
	stubFetch();
	const uninstall = transition.installPerformanceBrowserIpc();
	try {
		await assert.rejects(transition.engine.roll(1, 1), /no track loaded/);
		await transition.engine.load(1, SID);
		await assert.rejects(transition.engine.roll(1, 0.25), /paused/);
		assert.equal(transition.deckRollQuery(1).roll_stack.length, 0);
	} finally {
		uninstall();
	}
});

test('roll forces slip on engage and restores slip_enabled after release', async () => {
	installBrowserAudioGlobals();
	stubFetch();
	const uninstall = transition.installPerformanceBrowserIpc();
	try {
		await transition.engine.load(1, SID);
		await transition.engine.play(1);
		assert.equal(transition.deckStates[1].slip_enabled, false);
		await transition.engine.roll(1, 0.25);
		assert.equal(transition.deckStates[1].slip_active, true);
		assert.equal(transition.deckStates[1].loop?.engaged, true);
		assert.equal(transition.deckStates[1].loop?.beat_length, 0.25);
		assert.deepEqual(transition.deckRollQuery(1).roll_stack, [0.25]);
		await transition.engine.roll(1, null);
		assert.equal(transition.deckStates[1].slip_enabled, false);
		assert.equal(transition.deckStates[1].slip_active, false);
		assert.equal(transition.deckStates[1].loop, null);
		assert.deepEqual(transition.deckRollQuery(1).roll_stack, []);
	} finally {
		uninstall();
	}
});

test('roll release keeps user slip_enabled armed after a held roll', async () => {
	installBrowserAudioGlobals();
	stubFetch();
	const uninstall = transition.installPerformanceBrowserIpc();
	try {
		await transition.engine.load(1, SID);
		await transition.engine.setSlip(1, true);
		await transition.engine.play(1);
		assert.equal(transition.deckStates[1].slip_enabled, true);
		await transition.engine.roll(1, 0.25);
		assert.equal(transition.deckStates[1].slip_active, true);
		await transition.engine.roll(1, null);
		assert.equal(transition.deckStates[1].slip_enabled, true);
		assert.equal(transition.deckStates[1].slip_active, false);
		assert.deepEqual(transition.deckRollQuery(1).roll_stack, []);
	} finally {
		uninstall();
	}
});

test('roll release re-engages a regular loop that was held before the roll', async () => {
	installBrowserAudioGlobals();
	stubFetch();
	const uninstall = transition.installPerformanceBrowserIpc();
	try {
		await transition.engine.load(1, SID);
		await transition.engine.play(1);
		await transition.engine.engageBeatLoop(1, 4);
		const st = transition.deckStates[1];
		assert.equal(st.loop?.engaged, true);
		const { in_ms, out_ms, beat_length } = st.loop;
		await transition.engine.roll(1, 0.25);
		assert.equal(st.slip_active, true);
		await transition.engine.roll(1, null);
		assert.equal(st.loop?.engaged, true);
		assert.equal(st.loop.beat_length, beat_length);
		assert.equal(st.loop.in_ms, in_ms);
		assert.equal(st.loop.out_ms, out_ms);
		assert.equal(st.slip_active, false);
		assert.deepEqual(transition.deckRollQuery(1).roll_stack, []);
	} finally {
		uninstall();
	}
});

test('roll stack resumes the outer beat loop before the final release', async () => {
	installBrowserAudioGlobals();
	stubFetch();
	const uninstall = transition.installPerformanceBrowserIpc();
	try {
		await transition.engine.load(1, SID);
		await transition.engine.play(1);
		await transition.engine.roll(1, 1);
		const st = transition.deckStates[1];
		const outerIn = st.loop.in_ms;
		const outerOut = st.loop.out_ms;
		assert.deepEqual(transition.deckRollQuery(1).roll_stack, [1]);
		await transition.engine.roll(1, 0.25);
		assert.equal(st.loop?.beat_length, 0.25);
		assert.deepEqual(transition.deckRollQuery(1).roll_stack, [1, 0.25]);
		await transition.engine.roll(1, null);
		assert.equal(st.loop?.beat_length, 1);
		assert.equal(st.loop.in_ms, outerIn);
		assert.equal(st.loop.out_ms, outerOut);
		assert.deepEqual(transition.deckRollQuery(1).roll_stack, [1]);
		await transition.engine.roll(1, null);
		assert.equal(st.loop, null);
		assert.deepEqual(transition.deckRollQuery(1).roll_stack, []);
	} finally {
		uninstall();
	}
});
