// PLAY-27 follow-up for Sol P1 r4202961938 (#5738). -Claude
// [if] a master handover re-syncs two playing followers and only one can be planned
//   [then] the plannable one is synced, the other KEEPS PLAYING unsynced, and nothing throws
//   [else stop].
// Driven through the REAL engine (stub stretch processor only) and the REAL planner, via
// BOTH setDeckMaster and the post-election re-join (master unload, reason 'unload').
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://play27-multi.example.test';
const STRETCH_STUB = fileURLToPath(new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url));
const SECONDS = 120;
// stable id -> BPM. 126 and 127 lock to each other and to 128; 160 is 27% off.
const BPM = { ['1'.repeat(40)]: 128, ['2'.repeat(40)]: 126, ['3'.repeat(40)]: 160, ['4'.repeat(40)]: 127 };
const [SID_128, SID_126, SID_160, SID_127] = Object.keys(BPM);

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
	define('navigator', { userAgent: 'play27-multi-test' });
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

const deck = (id) => audio.getDeckState(id);
/** The tempo a deck is actually playing at: its track BPM times its pitch. */
const bpmNow = (id) => BPM[deck(id).stable_id] * deck(id).pitch;
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

afterEach(async () => {
	if (originalFetch !== undefined) globalThis.fetch = originalFetch;
	installGlobals();
	await audio.engine.dispose();
});

/** Deck 1 (128) plays as master; followers start with BEAT SYNC lit. */
async function playSet(followers) {
	stubFetch();
	const loads = { 1: SID_128, ...followers };
	for (const [d, sid] of Object.entries(loads)) await audio.engine.load(Number(d), sid);
	deck(1).beat_sync_enabled = true; // so a later handover re-syncs deck 1 as a follower
	await audio.engine.play(1);
	await until(() => deck(1).playing && deck(1).audible && deck(1).is_master, "deck 1 audible as master");
	for (const d of Object.keys(followers).map(Number)) {
		await audio.engine.setBeatSync(d, true); // not playing yet: lights it; a failure here fails the test
		assert.equal(deck(d).beat_sync_enabled, true, `deck ${d} BEAT SYNC lit through the real setter`);
		await audio.engine.play(d); // PLAY-27: an unlockable deck starts unsynced here
		await until(() => deck(d).playing && deck(d).audible, `deck ${d} playing and audible`);
	}
}

const lines = [];
const realLog = { info: console.info, log: console.log, warn: console.warn, error: console.error };
/** Every console line the engine prints (toasts are recorded as perf-event lines). */
function captureConsole() {
	lines.length = 0;
	for (const k of Object.keys(realLog)) console[k] = (...a) => { lines.push(a.map(String).join(' ')); realLog[k](...a); };
}
function releaseConsole() { Object.assign(console, realLog); }
const redNotice = (d) => lines.some((l) => /toast-error/.test(l) && new RegExp(`Beat Sync cannot lock deck ${d}: tempo \\d+% away; playing unsynced`).test(l));

test('[PLAY-27] Sol r4202961938 via setDeckMaster: the plannable follower syncs, the unplannable one keeps playing unsynced', async () => {
	captureConsole();
	try {
		await playSet({ 2: SID_126, 3: SID_160 });
		assert.equal(deck(3).sync_error, null, 'a single refused follower that starts unsynced is no deck error (LOW thread :3396)');
		assert.ok(redNotice(3), 'the single refusal raises the red notice');
		lines.length = 0;
		await audio.engine.setDeckMaster(2); // followers: deck 1 (128 -> 126, plannable), deck 3 (160 -> 126, ratio 1.27, not)
		await wait(300);
	} finally { releaseConsole(); }
	assert.equal(deck(2).is_master, true, 'the handover stands');
	assert.equal(deck(1).playing, true, 'deck 1 still playing');
	assert.ok(Math.abs(bpmNow(1) - bpmNow(2)) < 0.25, `deck 1 synced to the new master (${bpmNow(1)} vs ${bpmNow(2)} BPM)`);
	assert.equal(deck(3).playing, true, 'if the unplannable follower is stopped then the handover silenced it');
	assert.equal(deck(3).pitch, 1, 'deck 3 plays unsynced, at its own tempo');
	assert.ok(Math.abs(bpmNow(3) - bpmNow(2)) > 20, `deck 3 is not at the master's tempo (${bpmNow(3)} vs ${bpmNow(2)})`);
	assert.match(deck(3).sync_error ?? '', /no phase-capable/, 'r4203758016 ruling: a PARTIAL failure brands the unlocked deck');
	assert.ok(redNotice(3), 'the partial failure raises the same red notice as a single one');
});

test('[PLAY-27] Sol r4202961938 via the post-election re-join: the plannable follower syncs, the unplannable one keeps playing unsynced', async () => {
	await playSet({ 2: SID_126, 3: SID_160, 4: SID_127 });
	await audio.engine.setDeckMaster(2); // establish deck 2 as master, then hand off away from it automatically
	await wait(200);
	assert.equal(deck(2).is_master, true);
	captureConsole();
	try {
		await audio.engine.unload(2); // reason 'unload': election, then the automatic re-join of the playing followers
		await until(() => deck(1).is_master || deck(3).is_master || deck(4).is_master, 'a new master elected');
		await wait(400);
	} finally { releaseConsole(); }
	const master = [1, 3, 4].find((d) => deck(d).is_master);
	assert.ok(master !== undefined);
	for (const d of [1, 3, 4]) assert.equal(deck(d).playing, true, `if deck ${d} stopped then the re-join silenced it`);
	const bpmOf = { 1: 128, 3: 160, 4: 127 };
	const lockable = [1, 3, 4].filter((d) => d !== master && Math.abs(bpmOf[d] / bpmNow(master) - 1) <= 0.16);
	const unlockable = [1, 3, 4].filter((d) => d !== master && !lockable.includes(d));
	assert.ok(lockable.length > 0 && unlockable.length > 0, `fixture must give one of each (master ${master})`);
	for (const d of lockable) assert.ok(Math.abs(bpmNow(d) - bpmNow(master)) < 0.25, `deck ${d} synced to deck ${master} (${bpmNow(d)} vs ${bpmNow(master)} BPM)`);
	for (const d of unlockable) {
		assert.equal(deck(d).playing, true);
		assert.ok(Math.abs(bpmNow(d) - bpmNow(master)) > 20, `deck ${d} is not at the master's tempo`);
		assert.match(deck(d).sync_error ?? '', /no phase-capable/, `deck ${d}: a partial failure brands the unlocked deck`);
		assert.ok(redNotice(d), `deck ${d}: the re-join raises the red notice`);
	}
});
