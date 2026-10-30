// Perf win #3 (perf plan, Wed 7 Oct 2026): a deck load starts decoding the mix
// the moment its audio bytes land, instead of after the slowest of the four
// critical-path fetches (track, audio, ANLZ, hot cues).
//
// Wiring check against the recording stand-in in fixtures/fake-web-audio.mjs,
// with a stubbed daemon and stretch processor, the same harness as
// iopin-12-stall-rebuild.test.mjs. It proves the ORDER of calls, not a
// wall-clock gain; the gain needs a Mac measurement (press-to-playable).
//
// [if] the ANLZ answer is still in flight when the audio bytes land [then] the
//   decode has already started, and the load completes once ANLZ answers
//   [⛔️ if decode waits for ANLZ, the win is gone]
// [if] two fetches fail [then] the load rejects with whichever failed FIRST,
//   exactly as the old Promise.all over the four fetches did (control:
//   overlapping the decode must not change which error the DJ sees, in either
//   order)
// [if] the audio fetch fails while the metadata is fine [then] the load
//   rejects with the audio error
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { FakeAudioContext, installWindow } from './fixtures/fake-web-audio.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://decode-on-bytes.example.test';
const STRETCH_STUB = fileURLToPath(new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url));
const realFetch = globalThis.fetch;
const realDecode = FakeAudioContext.prototype.decodeAudioData;

let audio;
let stores;
let events = [];

function json(body, status = 200) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function anlzBody(sid) {
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
		local_waveform: { status: 'decoded' }
	};
}

/** A daemon whose ANLZ and audio answers the case controls. */
function installDaemon({ anlz, audioResponse }) {
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		if (url.includes('/anlz?')) {
			events.push('anlz-requested');
			const response = await anlz(url.match(/[0-9a-f]{40}/)[0]);
			events.push('anlz-answered');
			return response;
		}
		if (url.endsWith('/hot-cues')) {
			return json(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({ slot, cue: null, revision: `empty-${slot}` })));
		}
		if (url.endsWith('/audio')) return audioResponse();
		const track = url.match(/\/tracks\/([0-9a-f]{40})(\/stems)?$/);
		if (track?.[2] !== undefined) return json({ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } }, 404);
		if (track !== null) return json({ stable_id: track[1], title: 'Decode On Bytes', artist: 'Fixture', bpm: null });
		throw new Error(`unexpected request ${url}`);
	};
}

before(async () => {
	installWindow('');
	globalThis.AudioContext = FakeAudioContext;
	globalThis.requestAnimationFrame = (callback) => setTimeout(() => callback(performance.now()), 16);
	globalThis.cancelAnimationFrame = (handle) => clearTimeout(handle);
	FakeAudioContext.prototype.decodeAudioData = async function (bytes) {
		events.push('decode-started');
		return realDecode.call(this, bytes);
	};
	const entry = await loadTypeScriptModule('tests/unit/fixtures/djio-fallback-entry.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/rb/stretch-adapter': STRETCH_STUB }
	});
	({ audio, stores } = entry);
});

beforeEach(async () => {
	await audio.engine.dispose();
	FakeAudioContext.instances = [];
	events = [];
	for (const toast of [...stores.toasts]) stores.dismissToast(toast.logId);
});

afterEach(() => {
	globalThis.fetch = realFetch;
});

after(async () => {
	await audio.engine.dispose();
	for (const toast of [...stores.toasts]) stores.dismissToast(toast.logId);
	FakeAudioContext.prototype.decodeAudioData = realDecode;
	delete globalThis.AudioContext;
	delete globalThis.requestAnimationFrame;
	delete globalThis.cancelAnimationFrame;
	delete globalThis.window;
});

test('the mix decode starts while the ANLZ answer is still in flight, and the load completes once it lands', async () => {
	const sid = '1'.repeat(40);
	let releaseAnlz;
	const anlzGate = new Promise((resolve) => {
		releaseAnlz = resolve;
	});
	installDaemon({
		anlz: async (id) => {
			await anlzGate;
			return json(anlzBody(id));
		},
		audioResponse: () => new Response(new Uint8Array(4 * 4800))
	});
	const loading = audio.engine.load(1, sid);
	for (let i = 0; i < 200 && !events.includes('decode-started'); i++) await new Promise((resolve) => setImmediate(resolve));
	assert.ok(events.includes('decode-started'), 'decode never started while ANLZ was held');
	assert.ok(!events.includes('anlz-answered'), 'precondition: ANLZ is still in flight');
	releaseAnlz();
	await loading;
	assert.equal(audio.getDeckState(1).stable_id, sid);
	assert.ok(events.indexOf('decode-started') < events.indexOf('anlz-answered'));
});

async function settle(n) {
	for (let i = 0; i < n; i++) await new Promise((resolve) => setImmediate(resolve));
}

test('control: when the audio fetch fails before ANLZ, the DJ sees the audio error (first failure wins)', async () => {
	installDaemon({
		anlz: async () => {
			await settle(20);
			return json({ detail: { code: 'ANALYSIS_NOT_FOUND', message: 'track has no analysis' } }, 404);
		},
		audioResponse: () => json({ detail: { code: 'AUDIO_NOT_FOUND', message: 'no audio file' } }, 404)
	});
	await assert.rejects(audio.engine.load(1, '2'.repeat(40)), /AUDIO_NOT_FOUND|no audio file/);
	assert.equal(audio.getDeckState(1).stable_id, null);
});

test('control: when ANLZ fails before the audio fetch, the DJ sees the analysis error', async () => {
	installDaemon({
		anlz: async () => json({ detail: { code: 'ANALYSIS_NOT_FOUND', message: 'track has no analysis' } }, 404),
		audioResponse: async () => {
			await settle(20);
			return json({ detail: { code: 'AUDIO_NOT_FOUND', message: 'no audio file' } }, 404);
		}
	});
	await assert.rejects(audio.engine.load(1, '4'.repeat(40)), (error) => error?.code === 'ANALYSIS_NOT_FOUND');
	assert.equal(audio.getDeckState(1).stable_id, null);
});

test('an audio fetch failure with good metadata rejects the load with the audio error', async () => {
	installDaemon({
		anlz: async (id) => json(anlzBody(id)),
		audioResponse: () => json({ detail: { code: 'AUDIO_NOT_FOUND', message: 'no audio file' } }, 404)
	});
	await assert.rejects(audio.engine.load(1, '3'.repeat(40)), /AUDIO_NOT_FOUND|no audio file/);
	assert.ok(!events.includes('decode-started'));
	assert.equal(audio.getDeckState(1).stable_id, null);
});

// [if] a metadata fetch fails while the audio bytes are still in flight [then]
//   the bytes landing afterwards build no graph and start no decode - broken
//   otherwise (a dead load creates the AudioContext and a worklet; Codex P1 on #6127)
// [if] the engine is disposed while the bytes are in flight [then] the same
//   holds: dispose must not be undone by a late byte stage

test('bytes that land after a metadata failure build no graph for the dead load', async () => {
	let releaseAudio;
	const audioGate = new Promise((resolve) => {
		releaseAudio = resolve;
	});
	installDaemon({
		anlz: async () => json({ detail: { code: 'ANALYSIS_NOT_FOUND', message: 'track has no analysis' } }, 404),
		audioResponse: async () => {
			await audioGate;
			return new Response(new Uint8Array(4 * 4800));
		}
	});
	await assert.rejects(audio.engine.load(1, '5'.repeat(40)), (error) => error?.code === 'ANALYSIS_NOT_FOUND');
	releaseAudio();
	await settle(50);
	assert.ok(!events.includes('decode-started'), 'a failed load still decoded its late bytes');
	assert.equal(FakeAudioContext.instances.length, 0, 'a failed load still built an AudioContext');
});

test('bytes that land after engine.dispose() build no graph', async () => {
	let releaseAudio;
	const audioGate = new Promise((resolve) => {
		releaseAudio = resolve;
	});
	installDaemon({
		anlz: async (id) => json(anlzBody(id)),
		audioResponse: async () => {
			await audioGate;
			return new Response(new Uint8Array(4 * 4800));
		}
	});
	const loading = audio.engine.load(1, '6'.repeat(40)).then(
		() => 'loaded',
		() => 'rejected'
	);
	await settle(10);
	await audio.engine.dispose();
	releaseAudio();
	assert.equal(await loading, 'rejected');
	await settle(50);
	assert.ok(!events.includes('decode-started'), 'a disposed engine still decoded the late bytes');
	assert.equal(FakeAudioContext.instances.length, 0, 'a disposed engine rebuilt an AudioContext');
});

// [if] a metadata fetch fails after the mix worklet was built and that
//   worklet's dispose rejects (ack timeout after the context closed) [then]
//   the rejection is caught and logged, and the DJ still sees the metadata
//   error - broken otherwise (an unhandled rejection; Codex and Sol P2 on #6127)
test('a rejecting orphan dispose is caught and never replaces the metadata error', async () => {
	const unhandled = [];
	const onUnhandled = (reason) => unhandled.push(reason);
	process.on('unhandledRejection', onUnhandled);
	const warned = [];
	const realWarn = console.warn;
	console.warn = (...args) => warned.push(args);
	globalThis.__stretchStubDisposeError = new Error('worklet ack timed out');
	try {
		installDaemon({
			anlz: async () => {
				for (let i = 0; i < 200 && !events.includes('decode-started'); i++) await new Promise((resolve) => setImmediate(resolve));
				await settle(20);
				return json({ detail: { code: 'ANALYSIS_NOT_FOUND', message: 'track has no analysis' } }, 404);
			},
			audioResponse: () => new Response(new Uint8Array(4 * 4800))
		});
		await assert.rejects(audio.engine.load(1, '7'.repeat(40)), (error) => error?.code === 'ANALYSIS_NOT_FOUND');
		assert.ok(events.includes('decode-started'), 'precondition: the mix worklet was built before the failure');
		await settle(50);
		assert.deepEqual(unhandled, [], 'the dispose rejection went unhandled');
		assert.ok(
			warned.some((args) => String(args[0]).includes('orphan mix processor dispose failed')),
			'the dispose failure was swallowed without a word'
		);
	} finally {
		delete globalThis.__stretchStubDisposeError;
		console.warn = realWarn;
		process.off('unhandledRejection', onUnhandled);
	}
});
