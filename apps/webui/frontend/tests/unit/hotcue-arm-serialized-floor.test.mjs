// Codex P1 on PR #6834 (r4287632389). -Claude
// [if] a hot cue arms while an earlier schedule on the same deck is still in
//   flight [then] its downbeat is resolved on the floor the serialized schedule
//   actually lands on, and the countdown time returned is the time sent to the
//   processor, not a plan made before the wait [else stop].
// Driven through the REAL engine and planner; only the stretch processor is a
// stub, one that can hold a schedule in flight.
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://hotcue-arm-serial.example.test';
const STRETCH_STUB = fileURLToPath(new URL('./fixtures/stretch-deck-processor-gated-stub.ts', import.meta.url));
const SECONDS = 120;
const BPM = { ['1'.repeat(40)]: 128 };
const [SID] = Object.keys(BPM);

let audio;
let originalFetch;

const grid = (bpm) => {
	const out = [];
	for (let i = 0; (i * 60) / bpm < SECONDS - 1; i++) out.push({ n: (i % 4) + 1, bpm, t: 0.05 + (i * 60) / bpm });
	return out;
};
const param = (value = 1) => ({ value, setValueAtTime(v) { this.value = v; }, setTargetAtTime(v) { this.value = v; }, linearRampToValueAtTime() {}, cancelScheduledValues() {} });
const define = (name, value) => Object.defineProperty(globalThis, name, { value, configurable: true, writable: true, enumerable: true });

function installGlobals() {
	const localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
	define('window', { location: { href: `${API_BASE}/performance`, search: '' }, localStorage, addEventListener() {}, removeEventListener() {} });
	define('navigator', { userAgent: 'hotcue-arm-serial-test' });
	define('requestAnimationFrame', (cb) => setTimeout(() => cb(performance.now()), 16));
	define('cancelAnimationFrame', (id) => clearTimeout(id));
	define('localStorage', localStorage);
	define('AudioWorkletNode', function AudioWorkletNode() { return { connect() {}, disconnect() {}, port: { onmessage: null }, addEventListener() {}, removeEventListener() {} }; });
	define('Audio', function Audio() { return { pause() {}, play: async () => {}, srcObject: null, setSinkId: async () => {} }; });
	define('AudioContext', class AudioContext {
		constructor() {
			this.state = 'running';
			this.sampleRate = 48_000;
			this.baseLatency = 128 / 48_000;
			this.outputLatency = 0.032;
			this._t0 = performance.now();
			this.destination = { maxChannelCount: 2, channelCount: 2, channelInterpretation: 'speakers', connect() {}, disconnect() {} };
			this.audioWorklet = { addModule: async () => {} };
		}
		/** A real-time audio clock, so schedules land and transport advances. */
		get currentTime() { return (performance.now() - this._t0) / 1000; }
		#node(extra = {}) { return { context: this, connect(n) { return n; }, disconnect() {}, gain: param(), frequency: param(1000), Q: param(1), type: 'lowpass', delayTime: param(0), channelCount: 2, channelCountMode: 'max', channelInterpretation: 'speakers', ...extra }; }
		createGain() { return this.#node(); }
		createAnalyser() { return this.#node({ fftSize: 2048, frequencyBinCount: 1024, getFloatTimeDomainData() {}, getFloatFrequencyData() {} }); }
		createBiquadFilter() { return this.#node(); }
		createChannelSplitter() { return this.#node(); }
		createChannelMerger() { return this.#node(); }
		createDelay() { return this.#node(); }
		createConstantSource() {
			const ctx = this;
			const src = { context: ctx, offset: param(0), onended: null, connect: (n) => n, disconnect() {}, start() {},
				stop(when) { setTimeout(() => src.onended?.(), Math.max(0, (when - ctx.currentTime) * 1000)); } };
			return src;
		}
		createMediaStreamDestination() { return { context: this, stream: { getTracks: () => [] }, connect() {}, disconnect() {} }; }
		createBuffer(channels, length, sampleRate) { return { numberOfChannels: channels, length, sampleRate, duration: length / sampleRate, getChannelData: () => new Float32Array(length) }; }
		async decodeAudioData() {
			const length = 48_000 * SECONDS;
			return { duration: SECONDS, length, numberOfChannels: 2, sampleRate: 48_000, getChannelData: () => new Float32Array(16) };
		}
		getOutputTimestamp() { return { contextTime: this.currentTime, performanceTime: performance.now() }; }
		addEventListener() {}
		removeEventListener() {}
		async resume() {}
		async close() {}
	});
}

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
function stubFetch() {
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		const sid = Object.keys(BPM).find((s) => url.includes(s));
		if (sid === undefined) throw new Error(`unexpected request ${url}`);
		if (url.includes('/anlz?')) {
			const beats = grid(BPM[sid]);
			const bands = { length: 0, low: [], mid: [], high: [] };
			return json({ stable_id: sid, points: 38_400, waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } }, beatgrid: { source: 'rekordbox', status: 'ok', beat_count: beats.length, beats }, beatgrid_source: 'rekordbox', cues: [], phrases: [], vocals: { status: 'not_analyzed' }, local_waveform: { status: 'decoded' } });
		}
		if (url.endsWith('/hot-cues')) return json(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({ slot, cue: null, revision: `empty-${slot}` })));
		if (url.endsWith('/audio')) return new Response(new Uint8Array(4096));
		if (url.endsWith('/stems')) return json({ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } }, 404);
		if (url.endsWith(`/tracks/${sid}`)) return json({ stable_id: sid, title: `bpm ${BPM[sid]}`, artist: 'Fixture', bpm: BPM[sid], key: '8A' });
		throw new Error(`unexpected request ${url}`);
	};
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(pred, label, ms = 4000) {
	const end = Date.now() + ms;
	while (Date.now() < end) { if (pred()) return; await wait(20); }
	assert.fail(`timed out waiting for ${label}`);
}

before(async () => {
	originalFetch = globalThis.fetch;
	installGlobals();
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts', { viteApiBase: API_BASE, alias: { '$lib/rb/stretch-adapter': STRETCH_STUB } });
});

after(async () => {
	await audio.engine.dispose();
	if (originalFetch !== undefined) globalThis.fetch = originalFetch;
});

test('a hot cue armed behind an in-flight schedule re-plans on the serialized floor and returns the time it sent', async () => {
	stubFetch();
	const stub = globalThis.__gatedStretchStub;
	assert.ok(stub, 'the gated stretch stub was not bundled');
	await audio.engine.load(1, SID);
	await audio.engine.play(1);
	await until(() => audio.getDeckState(1).playing && audio.getDeckState(1).audible, 'deck 1 audible');

	// An earlier schedule on deck 1 is held in flight past the arm's plan.
	const release = stub.holdNextSchedule();
	const predecessor = audio.engine.quantizedSeek(1, 30_000, true);
	await until(() => stub.scheduleCalls.length > 0 && stub.scheduleCalls.at(-1).outputTime > 0, 'the held schedule reached the processor');
	const callsBefore = stub.scheduleCalls.length;

	let released = false;
	const resolverCalls = [];
	const nextBeat = (nowPositionSec) => {
		resolverCalls.push({ nowPositionSec, afterRelease: released });
		return Math.ceil((nowPositionSec - 0.05) / (60 / 128) + 1e-6) * (60 / 128) + 0.05;
	};
	const armed = audio.engine.armHotCueTrigger(1, 60_000, nextBeat);
	await wait(300); // the safety floor moves on while the predecessor is held
	released = true;
	release();
	await predecessor;
	const landed = await armed;

	const sent = stub.scheduleCalls.slice(callsBefore);
	assert.equal(sent.length, 1, `the arm sent ${sent.length} schedules`);
	assert.ok(resolverCalls.length === 1 && resolverCalls.every((c) => c.afterRelease),
		`the downbeat was resolved before the queued schedule ran: ${JSON.stringify(resolverCalls)}`);
	// The seek landed at 30 s, so the resolver saw the post-seek timeline.
	assert.ok(resolverCalls.at(-1).nowPositionSec >= 30, `the resolver saw ${resolverCalls.at(-1).nowPositionSec}s, before the seek`);
	assert.ok(Math.abs(landed - sent[0].outputTime) < 1e-9,
		`the countdown target ${landed} is not the time sent to the processor ${sent[0].outputTime}`);
});
