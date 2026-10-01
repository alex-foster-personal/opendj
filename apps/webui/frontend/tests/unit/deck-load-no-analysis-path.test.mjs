/**
 * Issue #2346: mapped present tracks with no rekordbox AnalysisDataPath must
 * load (never ANALYSIS_NOT_FOUND).
 *
 * [if] /anlz 200 for a mapped track with no AnalysisDataPath [then] load
 * publishes the deck and Play is enableable, [else stop].
 *
 * Backend contract: tests/webui/test_anlz_no_analysis_data_path.py.
 * Deferred grid upgrade: deck-beatgrid-fallback-upgrade.test.mjs.
 * H10 / other 404 rejection: audio-engine-controller.test.mjs.
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://deck-load-no-analysis.example.test';
const SID = 'a'.repeat(40);
const STRETCH_STUB = fileURLToPath(
	new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url)
);

let audio;
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

function makeNode(extra = {}) {
	return {
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
	defineGlobal('navigator', { userAgent: 'deck-load-no-analysis-path-test' });
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
	defineGlobal('AudioContext', class AudioContext {
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
			return { context: this, connect() {}, disconnect() {}, gain: makeAudioParam(), frequency: makeAudioParam(1000), Q: makeAudioParam(1), type: 'lowpass', delayTime: makeAudioParam(0), channelCount: 2, channelCountMode: 'max', channelInterpretation: 'speakers', ...extra };
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
	});
}

function emptyRescuedAnlz({ ownBeats = false } = {}) {
	const bands = { length: 0, low: [], mid: [], high: [] };
	return {
		stable_id: SID,
		points: 38_400,
		waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
		beatgrid: ownBeats
			? {
					source: 'own',
					status: 'ok',
					beat_count: 1,
					beats: [{ n: 1, bpm: 120, t: 0 }]
				}
			: { source: 'rekordbox', beat_count: 0, beats: [] },
		beatgrid_source: 'rekordbox',
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

function stubFetch({ ownBeats = false } = {}) {
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		// The client sends no points= by default (#3739: the server owns the
		// default), so match the route, not a query value.
		if (url.includes('/anlz?')) {
			return json(emptyRescuedAnlz({ ownBeats }));
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
			return new Response(new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8]));
		}
		if (url.endsWith(`/tracks/${SID}/stems`)) {
			return json(
				{ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } },
				404
			);
		}
		if (url.endsWith(`/tracks/${SID}`)) {
			return json({
				stable_id: SID,
				title: 'No Analysis Path',
				artist: 'Fixture',
				bpm: null
			});
		}
		throw new Error(`unexpected request ${url}`);
	};
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserAudioGlobals();
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts', {
		viteApiBase: API_BASE,
		alias: {
			'$lib/rb/stretch-adapter': STRETCH_STUB
		}
	});
});

afterEach(async () => {
	if (originalFetch !== undefined) globalThis.fetch = originalFetch;
	installBrowserAudioGlobals();
	await audio.engine.dispose();
});

test('a mapped no-AnalysisDataPath /anlz 200 publishes the deck on load', async () => {
	originalFetch = globalThis.fetch;
	stubFetch();
	await audio.engine.load(1, SID);
	assert.equal(audio.getDeckState(1).stable_id, SID);
	assert.equal(audio.deckLoadErrors[1], null);
	assert.equal(audio.getDeckState(1).anlz?.local_waveform?.status, 'decoded');
});

test('when the rescued payload already carries own beats, the deck adopts them', async () => {
	originalFetch = globalThis.fetch;
	stubFetch({ ownBeats: true });
	await audio.engine.load(1, SID);
	assert.equal(audio.getDeckState(1).stable_id, SID);
	assert.equal(audio.deckLoadErrors[1], null);
	assert.equal(audio.getDeckState(1).anlz?.beatgrid?.source, 'own');
	assert.ok((audio.getDeckState(1).anlz?.beatgrid?.beats?.length ?? 0) > 0);
});
