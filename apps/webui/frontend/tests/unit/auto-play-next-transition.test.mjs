// requirement: PLAY-11 (issue #3532)
// [if] AutoPlay Next armed then aborted on a loaded deck [then] fader commands still change nodes.fader.gain, [else stop].
// [if] AutoPlay Next arms on a playing deck away from loop-in [then] beat_loop lands on a downbeat via exactBeatLoopRangeMs, [else stop].
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://autoplay-next-transition.example.test';
const SID_OUT = 'a'.repeat(40);
const SID_IN = 'b'.repeat(40);
// Stub decodeAudioData derives duration from byteLength/4 at 48 kHz; keep
// this long enough for the 32-beat fixture (~15 s) used by AutoPlay Next.
const STUB_AUDIO_BYTES = 48_000 * 4 * 20;
const STRETCH_STUB = fileURLToPath(
	new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url)
);

let transition;
let loops;
let autoPlayNextPure;
let originalFetch;

function _beats(n) {
	const out = [];
	for (let i = 0; i < n; i++) out.push({ n: (i % 4) + 1, bpm: 128, t: i * 0.469 });
	return out;
}

function _flatWaveform(length) {
	return {
		kind: 'tri',
		preview: { length: 1, low: [0], mid: [0], high: [0] },
		detail: {
			length,
			low: new Array(length).fill(0.4),
			mid: new Array(length).fill(0.3),
			high: new Array(length).fill(0.2)
		}
	};
}

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
	const localStorage = {
		getItem: () => null,
		setItem: () => {},
		removeItem: () => {}
	};
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
		value: { userAgent: 'autoplay-next-transition-test' },
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
	Object.defineProperty(globalThis, 'Audio', {
		value: function Audio() {
			return { pause() {}, play: async () => {}, srcObject: null, setSinkId: async () => {} };
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
				this.currentTime = 0;
				this.baseLatency = 128 / 48_000;
				this.outputLatency = 0.032;
				this.destination = {
					maxChannelCount: 2,
					channelCount: 2,
					channelInterpretation: 'speakers',
					connect() {},
					disconnect() {}
				};
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
					type: 'lowpass',
					delayTime: makeAudioParam(0),
					channelCount: 2,
					channelCountMode: 'max',
					channelInterpretation: 'speakers',
					...extra
				};
			}
			createGain() {
				return this.#node();
			}
			createAnalyser() {
				return this.#node();
			}
			createBiquadFilter() {
				return this.#node();
			}
			createChannelSplitter() {
				return this.#node();
			}
			createChannelMerger() {
				return this.#node();
			}
			createDelay() {
				return this.#node();
			}
			createMediaStreamDestination() {
				return {
					context: this,
					stream: { getTracks: () => [] },
					connect() {},
					disconnect() {}
				};
			}
			createBuffer(channels, length, sampleRate) {
				return {
					numberOfChannels: channels,
					length,
					sampleRate,
					duration: length / sampleRate,
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
				return { contextTime: 0, performanceTime: performance.now() };
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

function anlzPayload(stableId) {
	const beats = _beats(32);
	return {
		stable_id: stableId,
		points: 38_400,
		waveform: _flatWaveform(3200),
		beatgrid: { source: 'own', status: 'ok', beat_count: beats.length, beats },
		beatgrid_source: 'own',
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' },
		local_waveform: { status: 'decoded' }
	};
}

function json(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

function _setupAutoPlayNextDecks(harness, { incomingBassAtDetailIndex = 500 } = {}) {
	const beats = _beats(32);
	const durationMs = (beats[beats.length - 1].t + 2) * 1000;
	const outgoingWaveform = _flatWaveform(3200);
	const incomingLow = new Array(3200).fill(0.05);
	for (let i = incomingBassAtDetailIndex; i < incomingLow.length; i++) incomingLow[i] = 0.9;
	const incomingWaveform = {
		kind: 'tri',
		preview: { length: 1, low: [0], mid: [0], high: [0] },
		detail: {
			length: 3200,
			low: incomingLow,
			mid: new Array(3200).fill(0.3),
			high: new Array(3200).fill(0.2)
		}
	};
	const outgoingAnlz = {
		waveform: outgoingWaveform,
		beatgrid: { source: 'own', status: 'ok', beat_count: beats.length, beats }
	};
	const incomingAnlz = {
		waveform: incomingWaveform,
		beatgrid: { source: 'own', status: 'ok', beat_count: beats.length, beats }
	};
	for (const deckId of [1, 2, 3, 4]) {
		const deck = harness.deckStates[deckId];
		deck.is_master = false;
		deck.playing = false;
		deck.loop = null;
		deck.slip_active = false;
	}
	harness.deckStates[1].stable_id = SID_OUT;
	harness.deckStates[1].playing = true;
	harness.deckStates[1].is_master = true;
	harness.deckStates[1].anlz = outgoingAnlz;
	harness.deckStates[1].duration_ms = durationMs;
	harness.deckStates[2].stable_id = SID_IN;
	harness.deckStates[2].anlz = incomingAnlz;
	harness.deckStates[2].duration_ms = durationMs;
	harness.deckStates[2].position_ms = 0;
	return { beats, durationMs };
}

function stubFetch() {
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		if (url.includes('/anlz?')) {
			if (url.includes(SID_OUT)) return json(anlzPayload(SID_OUT));
			if (url.includes(SID_IN)) return json(anlzPayload(SID_IN));
		}
		if (url.endsWith('/hot-cues')) {
			return json(
				['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({
					slot,
					cue: null,
					revision: `empty-${slot}`
				}))
			);
		}
		if (url.endsWith('/audio')) {
			return new Response(new Uint8Array(STUB_AUDIO_BYTES));
		}
		if (url.endsWith('/stems')) {
			return json({ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } }, 404);
		}
		if (url.endsWith(`/tracks/${SID_OUT}`) || url.endsWith(`/tracks/${SID_IN}`)) {
			return json({
				stable_id: url.endsWith(SID_OUT) ? SID_OUT : SID_IN,
				title: 'Fixture',
				artist: 'Fixture',
				bpm: 128
			});
		}
		throw new Error(`unexpected request ${url}`);
	};
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserAudioGlobals();
	[transition, loops, autoPlayNextPure] = await Promise.all([
		loadTypeScriptModule('tests/unit/fixtures/auto-play-next-transition-entry.ts', {
			viteApiBase: API_BASE,
			alias: {
				'$lib/rb/stretch-adapter': STRETCH_STUB
			}
		}),
		loadTypeScriptModule('src/lib/player/transport/loops.ts'),
		loadTypeScriptModule('src/lib/rb/auto-play-next.ts')
	]);
});

afterEach(async () => {
	await transition.cancelAutoPlayNext();
	if (originalFetch !== undefined) globalThis.fetch = originalFetch;
	installBrowserAudioGlobals();
	await transition.engine.dispose();
});

test('after AutoPlay Next armed then aborted, fader commands still change nodes.fader.gain', async () => {
	stubFetch();
	globalThis.window = {
		location: { href: `${API_BASE}/performance`, search: '' },
		localStorage: globalThis.localStorage
	};
	const uninstall = transition.installPerformanceBrowserIpc();
	try {
		await transition.engine.load(1, SID_OUT);
		await transition.engine.load(2, SID_IN);
		_setupAutoPlayNextDecks(transition);
		await transition.engine.play(1);
		assert.equal(await transition.armAutoPlayNext(), true);
		assert.notEqual(transition.deckStates[1].loop, null);
		await transition.cancelAutoPlayNext();
		assert.equal(transition.deckStates[1].loop, null);
		assert.equal(transition.deckStates[1].slip_active, false);
		assert.equal(transition.autoPlayNextState.armed, false);
		await transition.dispatchPerformanceCommand({ type: 'fader', deck: 1, value: 0.25 });
		assert.equal(transition.peekDeckFaderGain(1), 0.25);
	} finally {
		uninstall();
	}
});

test('armAutoPlayNext engages phase-aligned beat_loop on downbeat, not raw loop', async () => {
	stubFetch();
	globalThis.window = {
		location: { href: `${API_BASE}/performance`, search: '' },
		localStorage: globalThis.localStorage
	};
	const uninstall = transition.installPerformanceBrowserIpc();
	try {
		await transition.engine.load(1, SID_OUT);
		await transition.engine.load(2, SID_IN);
		const { beats, durationMs } = _setupAutoPlayNextDecks(transition);
		const durationSec = durationMs / 1000;
		const midBarMs = beats[5].t * 1000;
		await transition.engine.play(1);
		await transition.dispatchPerformanceCommand({ type: 'seek', deck: 1, position_ms: midBarMs });
		const window = autoPlayNextPure.findRepetitiveLoopWindow(
			transition.deckStates[1].anlz.waveform,
			beats,
			durationSec,
			autoPlayNextPure.DEFAULT_AUTO_PLAY_NEXT_CONFIG
		);
		const plan = autoPlayNextPure.planAutoPlayNextBeatLoop(beats, window, durationSec);
		assert.notEqual(plan, null);
		assert.equal(await transition.armAutoPlayNext(), true);
		const loop = transition.deckStates[1].loop;
		assert.ok(loop !== null);
		assert.equal(loop.beat_length, 8, 'beat_loop path sets beat_length; raw loop engage leaves it null');
		const expectedRange = loops.exactBeatLoopRangeMs(beats, midBarMs, plan.beats, plan.start_ms);
		assert.equal(loop.in_ms, expectedRange.in_ms);
		assert.equal(loop.out_ms, expectedRange.out_ms);
		const startIdx = beats.findIndex((beat) => beat.t * 1000 === loop.in_ms);
		assert.ok(startIdx >= 0);
		assert.equal(beats[startIdx].n, 1, 'loop-in must be on a PQTZ downbeat');
		assert.notEqual(loop.in_ms, midBarMs, 'loop-in is grid-anchored, not the arbitrary playhead');
		await transition.cancelAutoPlayNext();
	} finally {
		uninstall();
	}
});
