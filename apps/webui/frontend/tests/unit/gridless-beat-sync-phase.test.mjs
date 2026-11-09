/**
 * Issue #2607: Beat Sync and beat loops on a deck whose trusted grid has no
 * bar-1 downbeat stay inert, through the REAL deck-load path.
 *
 * The first version of this file wrote `deck.anlz` straight into the deck
 * state table. That never reached the guard: `_requireLoaded` rejects with
 * "no track loaded" before `requirePhaseCapableBeatGrid` runs when the deck
 * has no decoded processor, so the test could pass with the guard deleted.
 * Every case below loads the deck through `engine.load()` against stubbed
 * `/tracks`, `/anlz`, `/audio` and `/hot-cues` responses, the same way
 * deck-load-no-analysis-path.test.mjs does, and the control case proves the
 * loaded deck really reaches loop engagement when the grid does carry a
 * downbeat.
 *
 * [if] a loaded track has a structurally valid trusted grid without n===1
 *   [then] setBeatSync(true) resolves, keeps the DJ's flag, reports the
 *   missing downbeat as an info toast, and effective sync stays off ⛔️
 * [if] the same deck is asked for a beat loop (engine or typed command)
 *   [then] the rejection names the missing downbeat and is a plain Error,
 *   never `RangeError: no PQTZ downbeat (n===1) at or before ...`, and the
 *   UI wrapper persists that text as command_error ⛔️
 * [if] the same load path serves a grid WITH n===1 [then] the 4-beat loop
 *   engages (control: the guard, not the load path, is what refused above) ⛔️
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://gridless-beat-sync.example.test';
const SID = 'b'.repeat(40);
const STRETCH_STUB = fileURLToPath(
	new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url)
);
const BPM = 128;
const BEAT_SEC = 60 / BPM;
/** 30 s of stub audio: the decode stub reads one sample per 4 bytes at 48 kHz. */
const AUDIO_BYTES = 48_000 * 4 * 30;

/** Three beats at 128 BPM, cadence 2,3,4: the only shape `validateBeatGrid`
 * accepts that carries no bar-1 at all (a fourth beat would have to be n=1),
 * so this is the whole "trusted but phase-incapable" class, not a sample. */
function noDownbeatBeats() {
	return [2, 3, 4].map((n, index) => ({
		n,
		bpm: BPM,
		t: Number((0.1 + index * BEAT_SEC).toFixed(4))
	}));
}

/** 64 beats with a real PQTZ cadence 1,2,3,4, first downbeat at 100 ms. */
function downbeatBeats() {
	return Array.from({ length: 64 }, (_, index) => ({
		n: 1 + (index % 4),
		bpm: BPM,
		t: Number((0.1 + index * BEAT_SEC).toFixed(4))
	}));
}

let entry;
let originalFetch;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

function makeAudioParam(value = 1) {
	return {
		value,
		setValueAtTime() {},
		setTargetAtTime() {},
		linearRampToValueAtTime() {}
	};
}

function installBrowserAudioGlobals() {
	const localStorage = {
		getItem: () => null,
		setItem: () => {},
		removeItem: () => {}
	};
	defineGlobal('window', {
		location: { href: `${API_BASE}/performance`, search: '' },
		localStorage,
		addEventListener() {},
		removeEventListener() {}
	});
	defineGlobal('navigator', { userAgent: 'gridless-beat-sync-phase-test' });
	defineGlobal('localStorage', localStorage);
	defineGlobal('AudioWorkletNode', function AudioWorkletNode() {
		return {
			connect() {},
			disconnect() {},
			port: { onmessage: null },
			addEventListener() {},
			removeEventListener() {}
		};
	});
	defineGlobal('Audio', function Audio() {
		return {
			pause() {},
			play: async () => {},
			srcObject: null,
			setSinkId: async () => {}
		};
	});
	defineGlobal(
		'AudioContext',
		class AudioContext {
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
				this.audioWorklet = {
					addModule: async () => {}
				};
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
		}
	);
}

function anlzPayload(beats) {
	const bands = { length: 0, low: [], mid: [], high: [] };
	return {
		stable_id: SID,
		points: 38_400,
		waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
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

function stubFetch(beats) {
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		if (url.includes('/anlz?')) return json(anlzPayload(beats));
		if (url.endsWith('/hot-cues')) {
			return json(
				['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({
					slot,
					cue: null,
					revision: `empty-${slot}`
				}))
			);
		}
		if (url.endsWith('/audio')) return new Response(new Uint8Array(AUDIO_BYTES));
		if (url.endsWith(`/tracks/${SID}/stems`)) {
			return json({ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } }, 404);
		}
		if (url.endsWith(`/tracks/${SID}`)) {
			return json({ stable_id: SID, title: 'No Downbeat', artist: 'Fixture', bpm: BPM });
		}
		throw new Error(`unexpected request ${url}`);
	};
}

/** Load deck 1 for real and prove the load, so no case below asserts against
 * an unloaded deck by accident. */
async function loadDeck(beats) {
	stubFetch(beats);
	await entry.engine.load(1, SID);
	const st = entry.getDeckState(1);
	assert.equal(st.stable_id, SID, 'precondition: the deck loaded');
	assert.equal(entry.deckLoadErrors[1], null, 'precondition: load reported no error');
	assert.equal(entry.hasTrustedBeatGrid(st.anlz), true, 'precondition: the grid is trusted');
	return st;
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserAudioGlobals();
	entry = await loadTypeScriptModule('tests/unit/fixtures/gridless-beat-sync-entry.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/rb/stretch-adapter': STRETCH_STUB }
	});
});

afterEach(async () => {
	if (originalFetch !== undefined) globalThis.fetch = originalFetch;
	installBrowserAudioGlobals();
	await entry.engine.dispose();
	entry.toasts.length = 0;
});

test('a loaded deck with a trusted grid but no bar-1 downbeat is trusted for quantize, not phase work', async () => {
	const st = await loadDeck(noDownbeatBeats());
	assert.equal(entry.hasPhaseCapableBeatGrid(st.anlz), false);
});

test('setBeatSync(true) on that deck keeps the flag, toasts the missing downbeat, and engages nothing', async () => {
	const st = await loadDeck(noDownbeatBeats());
	const toastsBefore = entry.toasts.length;

	await entry.engine.setBeatSync(1, true);

	assert.equal(st.beat_sync_enabled, true, 'the DJ-chosen flag is kept, as setQuantize does');
	assert.equal(st.sync_error, null, 'an inert control is not a failed one');
	assert.equal(entry.effectiveBeatSync(st), false, 'the transport applies no phase lock');
	assert.equal(entry.toasts.length, toastsBefore + 1, 'exactly one announcement');
	const toast = entry.toasts.at(-1);
	assert.equal(toast.kind, 'info');
	assert.match(toast.message, /^Deck 1 BEAT SYNC needs a PQTZ downbeat/);
});

test('engageBeatLoop on that deck rejects with the named downbeat error, never a RangeError', async () => {
	await loadDeck(noDownbeatBeats());
	for (const beats of [4, 8]) {
		await assert.rejects(entry.engine.engageBeatLoop(1, beats), (error) => {
			assert.ok(error instanceof Error);
			assert.equal(
				error instanceof RangeError,
				false,
				`a ${beats}-beat loop surfaced the raw RangeError: ${error.message}`
			);
			// Exact prefix: "no track loaded" would also be a non-RangeError,
			// and it is precisely the rejection the first version of this test
			// mistook for the guard firing.
			assert.match(error.message, /^engageBeatLoop: deck 1 requires a PQTZ downbeat \(n===1\)/);
			return true;
		});
	}
	assert.equal(entry.getDeckState(1).loop, null, 'nothing was engaged on the way to the refusal');
});

test('armQuantizedLaunch on that deck refuses by name as well', async () => {
	await loadDeck(noDownbeatBeats());
	await assert.rejects(entry.engine.armQuantizedLaunch(1), (error) => {
		assert.ok(error instanceof Error);
		assert.equal(error instanceof RangeError, false, error.message);
		assert.match(error.message, /requires a PQTZ downbeat \(n===1\)/);
		return true;
	});
});

test('the typed beat_loop command lands the named refusal in command_error and the UI wrapper keeps it', async () => {
	await loadDeck(noDownbeatBeats());
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			entry.dispatchPerformanceCommand({ type: 'beat_loop', deck: 1, beats: 4 }),
			(error) => {
				assert.equal(error instanceof RangeError, false, String(error));
				assert.match(String(error), /requires a PQTZ downbeat \(n===1\)/);
				return true;
			}
		);
		const rejected = entry.queryPerformanceState().decks[1].command_error;
		assert.match(rejected ?? '', /requires a PQTZ downbeat \(n===1\)/);
		assert.doesNotMatch(rejected ?? '', /RangeError|at or before/);

		// The UI boundary consumes the rejection after persisting it.
		await entry.runPerformanceCommandFromUi({ type: 'beat_loop', deck: 1, beats: 8 });
		const wrapped = entry.queryPerformanceState().decks[1].command_error;
		assert.match(wrapped ?? '', /requires a PQTZ downbeat \(n===1\)/);
		assert.doesNotMatch(wrapped ?? '', /RangeError|at or before/);
		assert.match(entry.toasts.at(-1)?.message ?? '', /Performance command failed/i);

		// A typed beat_sync on the same deck is a refusal said out loud, not an error.
		const synced = await entry.dispatchPerformanceCommand({
			type: 'beat_sync',
			deck: 1,
			enabled: true
		});
		assert.equal(synced.decks[1].beat_sync_enabled, true);
		assert.equal(synced.decks[1].command_error, null);
		assert.match(entry.toasts.at(-1)?.message ?? '', /^Deck 1 BEAT SYNC needs a PQTZ downbeat/);
	} finally {
		uninstall();
	}
});

test('control: a fresh deck parked before its first downbeat engages the 4-beat loop on the nearest beat', async () => {
	// The other half of issue #2607, and the common one: every freshly loaded
	// deck sits at 0 ms with its first bar-1 some way in, and a four-beat loop
	// asked for there used to surface precedingDownbeatMs's RangeError.
	const st = await loadDeck(downbeatBeats());
	assert.equal(entry.hasPhaseCapableBeatGrid(st.anlz), true);
	assert.equal(st.position_ms, 0, 'precondition: parked before the 100 ms first downbeat');
	const toastsBefore = entry.toasts.length;

	await entry.engine.engageBeatLoop(1, 4);
	assert.notEqual(st.loop, null, 'the loop engaged through the loaded deck');
	assert.equal(st.loop.engaged, true);
	assert.equal(st.loop.beat_length, 4);
	assert.equal(st.loop.in_ms, 100, 'anchored on the nearest beat, the first one');
	assert.equal(st.loop.out_ms, Math.round((0.1 + 4 * BEAT_SEC) * 10_000) / 10, 'four beats long');
	assert.equal(entry.toasts.length, toastsBefore, 'a capable deck draws no inert toast');
});

test('control: past a downbeat the 4-beat loop still prefers the preceding bar-1 over the nearest beat', async () => {
	const st = await loadDeck(downbeatBeats());
	// Beat 5 (n=2) sits at 2443.75 ms and is the NEAREST beat to 2400 ms, so
	// the quantized seek parks on it; the preceding bar-1 is beat 4 at 1975 ms.
	// Only the downbeat rule picks 1975; nearest-beat would pick 2443.75.
	await entry.engine.quantizedSeek(1, 2400);
	assert.ok(
		st.position_ms > 1975 && st.position_ms <= 2444,
		`precondition: parked between the downbeat and beat 5, at ${st.position_ms} ms`
	);

	await entry.engine.engageBeatLoop(1, 4);
	assert.equal(st.loop.in_ms, 1975, 'the forgiving preceding-downbeat anchor is unchanged');
	assert.equal(st.loop.beat_length, 4);
});
