// requirement: STEM-4756
// [if] a stem bundle finishes decoding while its own deck plays [then] it takes over the deck without stopping it and the stems read ready, [else stop].
// [if] a stem bundle finishes decoding while its own deck plays [then] the live handoff keeps the deck audible, [else stop].
// [if] a deck's processor fails before its stem bundle finishes [then] the bundle is released and never lands on the cleared deck, [else stop].
/**
 * STEM-4756: a stem bundle that finishes on a playing deck takes over that deck.
 *
 * UPDATED for 5a3faf758b (on main, Thu 1 Oct 2026): a finished bundle takes over
 * a playing deck through a live handoff. The earlier rule ("held until the deck
 * next stopped") is the bug that commit fixed, so the assertions below expect the
 * handoff, not the hold.
 *
 * WHAT BROKE (installed build 7a441473a, measured Thu 1 Oct 2026 on bifrost1
 * and bifrost2, D:\odj-evidence\run-20261001-7a441473, check e): load a track,
 * press PLAY before the stem decode finishes, and the stems never reach
 * `ready`. 0 of 2 loads on each machine. The perf ring logged the decode as
 * done and held (`deck-stems ... deferredToStop=1`), and the status still read
 * `loading` 90 s after a real PAUSE click, with no toast and no error.
 *
 * WHY. The only place the held bundle was landed was one call at the end of
 * `engine.pause()`, straight after the pause schedule was ACKNOWLEDGED. A
 * pause is scheduled a transport lead into the future, so at that instant the
 * deck still holds the pending pause segment, its control clock is still
 * active, and `audible` / `transport_pending` have not been republished: the
 * replacement guard refuses, and nothing asked again. Every other way a deck
 * stops (CUE while playing, the track's natural end) had no landing at all.
 *
 * This file drives the REAL engine: real `load`, `play`, `pause`, `pressCue`,
 * the real presentation clock, the real replacement guard and the real stem
 * upgrade. Only the host platform node does not have is stood in for: an
 * AudioContext whose clock the test advances, requestAnimationFrame as a
 * queue the test steps, fetch, and the Signalsmith worklet adapter (a
 * recording stand-in, so a real processor swap can be told from a deck state
 * that merely says `ready`). It is the behavioral half that the structural
 * deck-lazy-stems.test.mjs could not give: that file asserted the drain call
 * EXISTED, which stayed true while it never worked.
 *
 * MUTATION CHECK, both directions (measured on the change that added this,
 * 7 tests):
 *   pre-fix engine (the only drain is in pause())          -> 5 red, 2 green
 *   (a) held work never released: the frame drain removed  -> 4 red
 *   (b) overshoot: the hold removed, swap while playing    -> 6 red
 *   (b) overshoot: the frame drain skips the rest guard    -> 3 red
 *   (b) overshoot: a failed deck keeps its held bundle     -> 1 red
 * The two controls that stay green on the pre-fix engine are the ones the
 * defect never touched: a deck at rest, and no swap under a playing deck.
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, beforeEach, describe, it } from 'node:test';

import { importBundledSource } from './import-bundled-source.mjs';
import { bundleTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://deck-stems-held-release.example.test';
const SID = 'c'.repeat(40);
const SAMPLE_RATE_HZ = 48_000;
// The fake decodeAudioData derives the frame count from byteLength / 4, so the
// mix and every stem part are the same length: the real alignment check passes
// for the reason it passes in the app (same frames, same rate).
const TRACK_SECONDS = 4;
const AUDIO_BYTES = SAMPLE_RATE_HZ * 4 * TRACK_SECONDS;
// What getOutputTimestamp reports behind the render clock: the device latency
// the presentation clock exists to honor.
const OUTPUT_LAG_S = 0.032;
const FRAME_S = 1 / 60;
const STRETCH_STUB = fileURLToPath(
	new URL('./fixtures/stretch-deck-processor-recording-stub.ts', import.meta.url)
);
const READY_MANIFEST = {
	schema: 1,
	stable_id: SID,
	source: 'demucs',
	model: 'hdemucs_mmi',
	layout: 'demucs4',
	sample_rate_hz: SAMPLE_RATE_HZ,
	frame_count: AUDIO_BYTES / 4,
	channel_count: 2,
	parts: {
		vocals: { media_type: 'audio/mpeg' },
		drums: { media_type: 'audio/mpeg' },
		bass: { media_type: 'audio/mpeg' },
		other: { media_type: 'audio/mpeg' }
	}
};

/** @type {string} */
let bundleText;
/** @type {Record<string, any>} */
let entry;
let originalFetch;
/** Render-clock seconds; the test is the only thing that advances it. */
let contextNowSec = 1;
/** requestAnimationFrame callbacks waiting for the next stepped frame. */
let frameQueue = [];
let nextFrameId = 1;
/** Resolves the four stem part downloads; held so the decode finishes on cue. */
let releaseStemParts = () => {};
let stemPartsRequested = 0;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, { value, configurable: true, writable: true, enumerable: true });
}

function makeAudioParam(value = 1) {
	return {
		value,
		setValueAtTime(next) {
			this.value = next;
		},
		setTargetAtTime(next) {
			this.value = next;
		},
		linearRampToValueAtTime() {},
		cancelScheduledValues() {}
	};
}

function fakePcmBuffer(length) {
	return {
		duration: length / SAMPLE_RATE_HZ,
		length,
		numberOfChannels: 2,
		sampleRate: SAMPLE_RATE_HZ,
		getChannelData: () => new Float32Array(length),
		copyFromChannel() {}
	};
}

function installBrowserAudioGlobals() {
	const localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
	defineGlobal('window', {
		location: { href: `${API_BASE}/performance`, search: '' },
		localStorage,
		addEventListener() {},
		removeEventListener() {}
	});
	// The engine reads the page query from the bare `location` global and has no
	// default by design (audio-engine.svelte.ts _stepLevelHandoff): stand in the page.
	defineGlobal('location', globalThis.window.location);
	defineGlobal('navigator', { userAgent: 'deck-stems-held-release-test' });
	defineGlobal('localStorage', localStorage);
	defineGlobal('Audio', function Audio() {
		return { pause() {}, play: async () => {}, srcObject: null, setSinkId: async () => {} };
	});
	// The xrun sentinel and the level meters are worklets; here they start and
	// never report, which is what an idle graph gives them.
	defineGlobal('AudioWorkletNode', function AudioWorkletNode() {
		return {
			connect() {},
			disconnect() {},
			port: { onmessage: null, postMessage() {}, close() {} },
			addEventListener() {},
			removeEventListener() {}
		};
	});
	defineGlobal('requestAnimationFrame', (callback) => {
		frameQueue.push(callback);
		return nextFrameId++;
	});
	defineGlobal('cancelAnimationFrame', () => {});
	defineGlobal(
		'AudioContext',
		class AudioContext {
			constructor() {
				this.state = 'running';
				this.sampleRate = SAMPLE_RATE_HZ;
				this.baseLatency = 128 / SAMPLE_RATE_HZ;
				this.outputLatency = OUTPUT_LAG_S;
				this.destination = {
					maxChannelCount: 2,
					channelCount: 2,
					channelInterpretation: 'speakers',
					connect() {},
					disconnect() {}
				};
				this.audioWorklet = { addModule: async () => {} };
			}
			get currentTime() {
				return contextNowSec;
			}
			#node() {
				return {
					context: this,
					connect() {},
					disconnect() {},
					gain: makeAudioParam(),
					frequency: makeAudioParam(1000),
					Q: makeAudioParam(1),
					type: 'lowpass',
					delayTime: makeAudioParam(0),
					fftSize: 2048,
					frequencyBinCount: 1024,
					getFloatTimeDomainData() {},
					getByteTimeDomainData() {},
					channelCount: 2,
					channelCountMode: 'max',
					channelInterpretation: 'speakers'
				};
			}
			createGain() {
				return this.#node();
			}
			createConstantSource() {
				return { ...this.#node(), offset: makeAudioParam(), start() {}, stop() {}, onended: null, connect(target) { return target; } };
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
				return { context: this, stream: { getTracks: () => [] }, connect() {}, disconnect() {} };
			}
			createBuffer(_channels, length) {
				return fakePcmBuffer(length);
			}
			async decodeAudioData(bytes) {
				return fakePcmBuffer(Math.max(1, Math.floor((bytes.byteLength ?? 0) / 4)));
			}
			getOutputTimestamp() {
				return {
					contextTime: Math.max(0, contextNowSec - OUTPUT_LAG_S),
					performanceTime: performance.now()
				};
			}
			addEventListener() {}
			removeEventListener() {}
			async resume() {}
			async close() {}
		}
	);
}

const json = (body, status = 200) =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/** A track with a real grid, so the deferred beatgrid upgrade has nothing to
 * fetch and the only background work after `load` is the stem upgrade. */
function griddedAnlz() {
	const bands = { length: 0, low: [], mid: [], high: [] };
	const beats = Array.from({ length: TRACK_SECONDS * 2 }, (_unused, i) => ({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 }));
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

function stubFetch() {
	const partsReleased = new Promise((resolve) => {
		releaseStemParts = resolve;
	});
	stemPartsRequested = 0;
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		if (url.includes('/anlz?')) return json(griddedAnlz());
		if (url.endsWith('/hot-cues')) {
			return json(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({ slot, cue: null, revision: `empty-${slot}` })));
		}
		if (url.endsWith('/audio')) return new Response(new Uint8Array(AUDIO_BYTES));
		if (url.endsWith(`/tracks/${SID}/stems`)) return json(READY_MANIFEST);
		if (url.includes(`/tracks/${SID}/stems/`)) {
			stemPartsRequested += 1;
			await partsReleased;
			return new Response(new Uint8Array(AUDIO_BYTES));
		}
		if (url.endsWith(`/tracks/${SID}`)) {
			return json({ stable_id: SID, title: 'Held Stems', artist: 'Fixture', bpm: 120 });
		}
		throw new Error(`unexpected request ${url}`);
	};
}

/** Let every promise continuation and zero-delay timer queued so far run. */
async function settle(turns = 4) {
	for (let i = 0; i < turns; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

async function waitFor(predicate, what) {
	for (let i = 0; i < 400 && !predicate(); i += 1) await settle(1);
	assert.ok(predicate(), `timed out waiting for ${what}`);
}

/** Advance the render clock one display frame and run the frame the engine queued. */
async function stepFrame() {
	contextNowSec += FRAME_S;
	const due = frameQueue;
	frameQueue = [];
	for (const callback of due) callback(performance.now());
	await settle(1);
}

async function stepFrames(count) {
	for (let i = 0; i < count; i += 1) await stepFrame();
}

/** Step frames until the deck's presented state matches, as a real display would. */
async function stepUntil(predicate, what) {
	for (let i = 0; i < 240 && !predicate(); i += 1) await stepFrame();
	assert.ok(predicate(), `the deck never reached: ${what}`);
}

const deck = () => entry.deckStates[1];
/** The row the stem upgrade writes when it finishes: one per upgrade, held or not. */
const upgradeRows = () => entry.readPerfEvents().filter((row) => row.kind.startsWith('deck-stems sid='));
const heldRows = () => upgradeRows().filter((row) => /deferredToStop=1/.test(row.message));
const tickFailures = () => entry.readPerfEvents().filter((row) => row.kind === 'presentation-tick-failed');
/** Every processor the engine created, in order: the mix first, then the stem parts. */
const processors = () => globalThis.__stretchStubProcessors;
const mixProcessor = () => processors()[0];
const stemProcessors = () => processors().slice(1);
const playSchedules = (processor) => processor.schedules.filter((sent) => sent.change.active === true);

/** Load deck 1 with its stem parts still downloading, press PLAY, wait until
 * the deck is audibly playing, then let the stems finish decoding. Returns
 * once the upgrade has finished, having checked the bundle was held for the
 * stop and not swapped in under the playing deck. */
async function loadPlayThenFinishStemDecode() {
	await entry.engine.load(1, SID);
	assert.equal(deck().stems.status, 'loading', 'the stems must still be loading when PLAY is pressed');
	await waitFor(() => stemPartsRequested === 4, 'the four stem part downloads to start');
	await entry.engine.play(1);
	await stepUntil(() => deck().playing && deck().audible && !deck().transport_pending, 'audibly playing');
	releaseStemParts();
	// 5a3faf758b: a bundle that finishes while the deck plays takes over the
	// deck without stopping it. Nothing is held for the next stop any more.
	await stepUntil(() => deck().stems.status === 'ready', 'the live handoff to the stems');
	assert.equal(deck().playing, true, 'the handoff stopped the deck');
	assert.equal(deck().audible, true, 'the handoff silenced the deck');
	assert.equal(heldRows().length, 0, 'the finished bundle was held instead of handed off');
	assert.equal(stemProcessors().length, 4, 'a demucs4 bundle builds one processor per part');
}

describe('a stem bundle that finishes on a playing deck takes over the deck (STEM-4756, 5a3faf758b)', { concurrency: false }, () => {
	before(async () => {
		originalFetch = globalThis.fetch;
		bundleText = await bundleTypeScriptModule('tests/unit/fixtures/deck-stems-held-release-entry.ts', {
			viteApiBase: API_BASE,
			alias: { '$lib/rb/stretch-adapter': STRETCH_STUB }
		});
		// Runes are compile-time in Svelte; under node `$state` is an identity
		// function, exactly as load-typescript.mjs defines it for every unit test.
		globalThis.__musicDjToolsTestState = (value) => value;
		globalThis.__musicDjToolsTestState.snapshot = (value) =>
			value === undefined ? undefined : JSON.parse(JSON.stringify(value));
	});

	beforeEach(async () => {
		contextNowSec = 1;
		frameQueue = [];
		globalThis.__stretchStubProcessors = [];
		installBrowserAudioGlobals();
		stubFetch();
		entry = await importBundledSource(bundleText, 'deck-stems-held-release-entry');
		entry.resetPerfEventLog();
	});

	afterEach(async () => {
		releaseStemParts();
		await entry.engine.dispose();
		assert.deepEqual(
			tickFailures().map((row) => row.message),
			[],
			'the real presentation frame threw under this harness, so nothing above measured the engine'
		);
	});

	after(() => {
		if (originalFetch !== undefined) globalThis.fetch = originalFetch;
	});

	it('control: a deck left at rest gets its stems as soon as they decode', { skip: 'quarantined: timing flake, fails on loaded runners, 3 main runs on Wed 4 Nov 2026 (latest b6eded3764), #8530' }, async () => {
		await entry.engine.load(1, SID);
		await waitFor(() => stemPartsRequested === 4, 'the four stem part downloads to start');
		releaseStemParts();
		await waitFor(() => deck().stems.status !== 'loading', 'the stem upgrade to settle');
		assert.equal(deck().stems.status, 'ready');
		assert.equal(heldRows().length, 0, 'a deck at rest has nothing to wait for');
	});

	it('control: a bundle that finishes while the deck plays takes over without stopping it (5a3faf758b)', { skip: 'quarantined: timing flake, fails on loaded runners, 3 main runs on Wed 4 Nov 2026 (latest b6eded3764), #8530' }, async () => {
		await loadPlayThenFinishStemDecode();
		await stepFrames(30);
		assert.equal(deck().playing, true, 'the deck stopped under the handoff');
		assert.equal(deck().audible, true, 'the deck went silent under the handoff');
		assert.equal(deck().stems.status, 'ready');
		assert.ok(
			stemProcessors().every((processor) => !processor.disposed),
			'a landed stem part was retired while the deck was still playing'
		);
	});

	it('PAUSE after a live handoff: the deck comes to rest and the stems stay ready', async () => {
		await loadPlayThenFinishStemDecode();
		const positionBeforePauseMs = deck().position_ms;
		await entry.engine.pause(1);
		await stepUntil(() => !deck().audible && !deck().transport_pending, 'at rest after PAUSE');
		assert.equal(deck().stems.status, 'ready', 'the stems were lost when the deck stopped');
		assert.deepEqual(deck().stems.available_controls, ['vocal', 'instrumental', 'drums', 'bass', 'other']);
		assert.ok(
			deck().position_ms >= positionBeforePauseMs,
			'the playhead moved backwards when the deck stopped: the DJ would resume from the wrong place'
		);
	});

	it('control: a deck whose processor fails before its stems finish never receives them', async () => {
		await entry.engine.load(1, SID);
		await waitFor(() => stemPartsRequested === 4, 'the four stem part downloads to start');
		await entry.engine.play(1);
		await stepUntil(() => deck().playing && deck().audible && !deck().transport_pending, 'audibly playing');
		// The worklet reports a render failure: the engine clears the deck, so the
		// stems that finish afterwards have no track to land on (and must not graft
		// onto a cleared deck just because it reads at rest).
		mixProcessor().options.onProcessorError(new Error('worklet render failed (test)'));
		assert.equal(deck().stable_id, null, 'a failed processor must clear the deck');
		releaseStemParts();
		await stepFrames(30);
		assert.notEqual(deck().stems.status, 'ready', 'stems read ready on a deck that has no track');
		assert.ok(
			stemProcessors().every((processor) => processor.disposed),
			'the finished stems were neither landed nor released: their worklets and PCM leak until the next load'
		);
	});

	it('control: PAUSE then PLAY again keeps the landed stems', async () => {
		await loadPlayThenFinishStemDecode();
		// AUDFID-02 (#8428): a neutral bundle is held and the deck plays the
		// original file; the first stem touch engages the stems PLAY must reach.
		assert.equal(deck().stems.playback, 'original', 'a neutral bundle must be held, the deck playing the original file');
		entry.engine.setStemGain(1, 'drums', 0.9);
		await stepUntil(() => deck().stems.playback === 'stems', 'the first stem touch to engage the held bundle');
		await entry.engine.pause(1);
		await entry.engine.play(1);
		await stepUntil(() => deck().playing && deck().audible && !deck().transport_pending, 'playing again');
		await stepFrames(30);
		assert.equal(deck().stems.status, 'ready', 'the stems were lost by PAUSE then PLAY');
		assert.ok(
			stemProcessors().every((processor) => playSchedules(processor).length >= 1),
			'PLAY did not reach every stem part'
		);
	});

	it('CUE while playing: the stems stay ready once the deck comes to rest at the cue', async () => {
		await loadPlayThenFinishStemDecode();
		await entry.engine.pressCue(1);
		await stepUntil(() => !deck().audible && !deck().transport_pending, 'at rest after CUE');
		assert.equal(deck().playing, false);
		assert.equal(deck().stems.status, 'ready', 'the stems were lost when CUE stopped the deck');
	});

	it('natural end: the stems stay ready once the track has played out', async () => {
		await loadPlayThenFinishStemDecode();
		// Jump the render clock past the end of the track, as a tab that was in
		// the background while the track finished would see it.
		contextNowSec += TRACK_SECONDS + 1;
		await stepUntil(
			() => !deck().playing && !deck().audible && !deck().transport_pending,
			'at rest after the natural end'
		);
		assert.equal(deck().stems.status, 'ready', 'the stems were lost when the track ended');
	});
});
