/**
 * The REAL audio engine under node, for tests that must observe behavior.
 *
 * Same method as deck-stems-held-release.test.mjs (STEM-39), lifted into a
 * module so more than one test file can drive it: real `load`, `play`, `pause`,
 * `pressCue`, the real presentation clock, the real replacement guard, the real
 * stem upgrade and the real unexpected-pause reporter. Only the host platform
 * node does not have is stood in for: an AudioContext whose clock the test
 * advances, requestAnimationFrame as a queue the test steps, fetch, and the
 * Signalsmith worklet adapter (the recording stand-in in
 * fixtures/stretch-deck-processor-recording-stub.ts, so a real processor swap
 * can be told from a deck state that merely says `ready`).
 *
 * What it adds over that file's inline copy: more than one track, each with its
 * own gates. A test can hold a track's audio download or its stem part
 * downloads and release them on cue, and can make a track's audio fail.
 *
 * A track can also be GRIDLESS (`grid: 'none'`), the shape a locally imported
 * file really gets from /anlz, so the deferred beatgrid upgrade (PARITY-10)
 * runs for real: /rb-meta and /beatgrid-fallback answer with the verified
 * backend captures deck-beatgrid-fallback-upgrade.test.mjs reads, and the
 * fallback request can be held and released on cue like the others. The grid
 * lands through the scoped sync runner, which the app installs from
 * performance-ipc.svelte.ts; that module cannot be bundled here (its circular
 * import with the engine, see background-shed-jobs.test.mjs), so the rig
 * installs the same shape over the real ScopedCommandScheduler.
 *
 * The STEM-39 file keeps its inline copy for now: it belongs to PR #4756, which
 * this work is stacked on, and moving its scaffolding from under an open pull
 * request would only manufacture a conflict.
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

import { loadVerifiedJsonCapture } from './fixtures/verified-capture.mjs';
import { importBundledSource } from './import-bundled-source.mjs';
import { bundleTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://real-engine-rig.example.test';
export const SAMPLE_RATE_HZ = 48_000;
// The fake decodeAudioData derives the frame count from byteLength / 4, so the
// mix and every stem part are the same length: the real alignment check passes
// for the reason it passes in the app (same frames, same rate).
export const TRACK_SECONDS = 4;
const AUDIO_BYTES = SAMPLE_RATE_HZ * 4 * TRACK_SECONDS;
// What getOutputTimestamp reports behind the render clock: the device latency
// the presentation clock exists to honor.
const OUTPUT_LAG_S = 0.032;
const FRAME_S = 1 / 60;
const STRETCH_STUB = fileURLToPath(
	new URL('./fixtures/stretch-deck-processor-recording-stub.ts', import.meta.url)
);
const HOT_CUE_SLOTS = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
/** Real /rb-meta and /beatgrid-fallback response bytes, hash-checked before use. */
const CAPTURED = loadVerifiedJsonCapture(
	new URL('./fixtures/beatgrid-fallback-unmapped-captured.json', import.meta.url),
	new URL('./fixtures/beatgrid-fallback-unmapped-captured.manifest.json', import.meta.url)
);
/** The grid a gridless track's fallback upgrade lands: the captured analysis beats. */
export const FALLBACK_BEATS = CAPTURED.beatgrid_fallback_ok.body.beatgrid.beats;

/** Bundle the engine once per test file; every test imports it afresh. */
export function bundleRealEngine() {
	return bundleTypeScriptModule('tests/unit/fixtures/real-engine-entry.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/rb/stretch-adapter': STRETCH_STUB }
	});
}

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

const json = (body, status = 200) =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/** A gate a request waits behind: open until `hold()`, then shut until `release()`. */
function makeGate(initiallyHeld) {
	let open = () => {};
	let opened = initiallyHeld ? new Promise((resolve) => (open = resolve)) : Promise.resolve();
	return {
		wait: () => opened,
		hold() {
			opened = new Promise((resolve) => (open = resolve));
		},
		release() {
			open();
		}
	};
}

/** A track with a real grid (the default), so the deferred beatgrid upgrade has
 * nothing to fetch and the only background work after `load` is the stem upgrade. */
function griddedAnlz(sid) {
	const bands = { length: 0, low: [], mid: [], high: [] };
	const beats = Array.from({ length: TRACK_SECONDS * 2 }, (_unused, i) => ({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 }));
	return {
		stable_id: sid,
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

/** What /anlz serves a locally imported file (apps.analysis_waveform
 * local_anlz_payload over rb_vendor.empty_anlz_payload): an empty rekordbox
 * grid and a `local_waveform` block. No peaks are invented: the decode is
 * reported as not run. */
function gridlessAnlz(sid) {
	const bands = { length: 0, low: [], mid: [], high: [] };
	return {
		stable_id: sid,
		points: 38_400,
		waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
		beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] },
		beatgrid_source: 'rekordbox',
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' },
		local_waveform: { status: 'not_decoded', reason: 'not decoded (test)', preview_b64: null, preview_max: null, retryable: false }
	};
}

/** A captured body re-addressed to `sid`: only the id differs from the real bytes. */
const capturedFor = (capture, sid) => json({ ...capture.body, stable_id: sid }, capture.status);

function stemManifest(sid) {
	return {
		schema: 1,
		stable_id: sid,
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
}

/**
 * Stand up the platform and import a fresh copy of the engine.
 *
 * `tracks` maps a 40-hex stable id to `{ title, audio, listed_duration_ms, grid, fallback }`.
 * `audio` is `'ok'` (default) or `'fail'` (the audio endpoint answers 500).
 * `grid` is `'real'` (default) or `'none'` (a locally imported file: /anlz has
 * no grid, so `load` starts the beatgrid fallback upgrade). `fallback` is what
 * /beatgrid-fallback answers for it: `'ok'` (default, the captured analysis
 * grid) or `'missing'` (the captured 404: no analysis yet).
 * `listed_duration_ms` is the library's listed length, when a test needs it to
 * differ from the 4 s that is actually decoded. Every track has a ready demucs4
 * stem bundle whose four part downloads are HELD until the test calls
 * `stemParts(sid).release()`, so the stem decode finishes on cue.
 *
 * @param {string} bundleText from bundleRealEngine()
 * @param {{ tracks: Record<string, { title: string, audio?: 'ok' | 'fail', listed_duration_ms?: number, grid?: 'real' | 'none', fallback?: 'ok' | 'missing' }> }} options
 */
export async function startRealEngine(bundleText, { tracks }) {
	/** Render-clock seconds; the test is the only thing that advances it. */
	let contextNowSec = 1;
	/** requestAnimationFrame callbacks waiting for the next stepped frame. */
	let frameQueue = [];
	let nextFrameId = 1;
	const originalFetch = globalThis.fetch;
	/** Dispose gates failNextSwap() holds, released by stop() if a test did not. */
	const swapDisposeGates = [];
	const perTrack = new Map(
		Object.entries(tracks).map(([sid, track]) => [
			sid,
			{
				...track,
				audioGate: makeGate(false),
				stemGate: makeGate(true),
				fallbackGate: makeGate(false),
				stemPartsRequested: 0,
				audioRequested: 0,
				rbMetaRequested: 0,
				fallbackRequested: 0
			}
		])
	);
	const trackOf = (sid) => {
		const track = perTrack.get(sid);
		if (track === undefined) throw new Error(`real-engine-rig: no track ${sid} was configured`);
		return track;
	};

	// Runes are compile-time in Svelte; under node `$state` is an identity
	// function, exactly as load-typescript.mjs defines it for every unit test.
	globalThis.__musicDjToolsTestState = (value) => value;
	globalThis.__musicDjToolsTestState.snapshot = (value) =>
		value === undefined ? undefined : JSON.parse(JSON.stringify(value));
	globalThis.__stretchStubProcessors = [];

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
	defineGlobal('navigator', { userAgent: 'real-engine-rig' });
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
			createWaveShaper() {
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

	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		const match = /\/tracks\/([0-9a-f]{40})(\/[^?]*)?(\?.*)?$/.exec(url);
		if (match === null) throw new Error(`real-engine-rig: unexpected request ${url}`);
		const sid = match[1];
		const rest = match[2] ?? '';
		const track = trackOf(sid);
		if (rest === '/anlz') return json(track.grid === 'none' ? gridlessAnlz(sid) : griddedAnlz(sid));
		if (rest === '/rb-meta') {
			track.rbMetaRequested += 1;
			return capturedFor(CAPTURED.rb_meta_ok, sid);
		}
		if (rest === '/beatgrid-fallback') {
			track.fallbackRequested += 1;
			await track.fallbackGate.wait();
			if (track.fallback === 'missing') return json(CAPTURED.beatgrid_fallback_not_found.body, CAPTURED.beatgrid_fallback_not_found.status);
			return capturedFor(CAPTURED.beatgrid_fallback_ok, sid);
		}
		if (rest === '/hot-cues') {
			return json(HOT_CUE_SLOTS.map((slot) => ({ slot, cue: null, revision: `empty-${slot}` })));
		}
		if (rest === '/audio') {
			track.audioRequested += 1;
			await track.audioGate.wait();
			if (track.audio === 'fail') {
				return json({ detail: { code: 'AUDIO_FILE_MISSING', message: 'the audio file is not on this machine (test)' } }, 500);
			}
			return new Response(new Uint8Array(AUDIO_BYTES));
		}
		if (rest === '/stems') return json(stemManifest(sid));
		if (rest.startsWith('/stems/')) {
			track.stemPartsRequested += 1;
			await track.stemGate.wait();
			return new Response(new Uint8Array(AUDIO_BYTES));
		}
		if (rest === '') {
			const listed = track.listed_duration_ms === undefined ? {} : { duration_ms: track.listed_duration_ms };
			return json({ stable_id: sid, title: track.title, artist: 'Fixture', bpm: 120, ...listed });
		}
		throw new Error(`real-engine-rig: unexpected request ${url}`);
	};

	const entry = await importBundledSource(bundleText, 'real-engine-entry');
	entry.resetPerfEventLog();
	// performance-ipc.svelte.ts's runner, without its status counters: claim the
	// deck, and widen to every deck plus 'sync' when a settlement asks to.
	const scheduler = new entry.ScopedCommandScheduler();
	entry.installScopedSyncRunner((deck, run) =>
		scheduler.run([deck], () => run((work) => scheduler.run([...entry.DECK_IDS, 'sync'], work)))
	);

	/** Let every promise continuation and zero-delay timer queued so far run. */
	async function settle(turns = 4) {
		for (let i = 0; i < turns; i += 1) await new Promise((resolve) => setImmediate(resolve));
	}

	/** Wall-clock floor for waitFor: a loaded CI runner can spend 400 turns in under 100 ms (STEM-4806, #10508). */
	const WAIT_FOR_MIN_MS = 5000;

	/** Turn the loop until `predicate` holds: at least 400 turns AND at least WAIT_FOR_MIN_MS of wall time. */
	async function waitFor(predicate, what) {
		const deadline = performance.now() + WAIT_FOR_MIN_MS;
		for (let i = 0; !predicate() && (i < 400 || performance.now() < deadline); i += 1) await settle(1);
		assert.ok(predicate(), `timed out waiting for ${what} (400 turns and ${WAIT_FOR_MIN_MS} ms)`);
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

	const ring = () => entry.readPerfEvents();

	return {
		entry,
		engine: entry.engine,
		deck: (id = 1) => entry.deckStates[id],
		loadError: (id = 1) => entry.deckLoadErrors[id],
		settle,
		waitFor,
		stepFrame,
		stepFrames,
		stepUntil,
		/** Jump the render clock, as a tab that was in the background would see it. */
		advanceClock(seconds) {
			contextNowSec += seconds;
		},
		/** Every processor the engine created, in creation order. */
		processors: () => globalThis.__stretchStubProcessors,
		/** Make the NEXT processor the engine creates throw at its swap's connect,
		 * and hold its dispose until released: a load whose swap fails on a deck
		 * that is not playing, with the window that failure leaves open held. */
		failNextSwap() {
			const registry = globalThis.__stretchStubProcessors;
			const disposeGate = makeGate(true);
			swapDisposeGates.push(disposeGate);
			let disposing = false;
			registry.push = (record) => {
				delete registry.push; // back to Array.prototype.push for every later processor
				record.connectError = new Error('connect failed (test)');
				record.beforeDispose = () => ((disposing = true), disposeGate.wait());
				return registry.push(record);
			};
			return { disposing: () => disposing, releaseDispose: () => disposeGate.release() };
		},
		/** Hold or release a track's audio download. Open unless held. */
		audio: (sid) => ({
			hold: () => trackOf(sid).audioGate.hold(),
			release: () => trackOf(sid).audioGate.release(),
			requested: () => trackOf(sid).audioRequested
		}),
		/** A track's four stem part downloads. HELD until released. */
		stemParts: (sid) => ({
			release: () => trackOf(sid).stemGate.release(),
			requested: () => trackOf(sid).stemPartsRequested
		}),
		/** A gridless track's beatgrid upgrade requests. The fallback is open unless held. */
		beatgridFallback: (sid) => ({
			hold: () => trackOf(sid).fallbackGate.hold(),
			release: () => trackOf(sid).fallbackGate.release(),
			requested: () => trackOf(sid).fallbackRequested,
			rbMetaRequested: () => trackOf(sid).rbMetaRequested
		}),
		ring,
		/** The row the stem upgrade writes when it finishes: one per upgrade, held or not. */
		stemUpgradeRows: () => ring().filter((row) => row.kind.startsWith('deck-stems sid=')),
		unexpectedPauseRows: () => ring().filter((row) => row.kind === 'audio-unexpected-pause'),
		errorToasts: () => entry.toasts.filter((toast) => toast.kind === 'error').map((toast) => toast.message),
		tickFailures: () => ring().filter((row) => row.kind === 'presentation-tick-failed'),
		/** Release every gate, dispose the engine and put fetch back. */
		async stop() {
			for (const track of perTrack.values()) {
				track.audioGate.release();
				track.stemGate.release();
				track.fallbackGate.release();
			}
			for (const gate of swapDisposeGates) gate.release();
			await entry.engine.dispose();
			globalThis.fetch = originalFetch;
		}
	};
}

/** Until the deck is audibly playing with nothing pending: what a DJ would call "playing". */
export const audiblyPlaying = (deck) => deck.playing && deck.audible && !deck.transport_pending;
/** Until the deck has come to rest: the state the replacement guard waits for. */
export const atRest = (deck) => !deck.playing && !deck.audible && !deck.transport_pending;
