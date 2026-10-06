import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * SET-12: "Master mix (internal)" set recording, the page half.
 *
 * Two subjects, both run for real:
 * - master-capture-processor.js, the AudioWorklet that taps the master bus,
 *   evaluated byte for byte under a faked worklet scope (as the xrun sentinel
 *   tests do): int16 conversion, chunking, silence for an idle bus, the flush,
 *   and that steady state allocates nothing on the audio thread;
 * - master-mix-capture.ts, which posts the chunks in order and fails loudly.
 *
 * Regression lines:
 * - if a quantum allocates once the pool is warm then the audio thread pays GC
 * - if a stopped bus writes nothing then the WAV drifts from the set's time
 * - if a refused chunk is skipped instead of stopping the tap then the set has a hole
 * - if chunks post out of order or with a reused seq then the daemon refuses the set
 */

const API_BASE = 'https://master-mix.example.test';
const ENGINE_STUB = fileURLToPath(new URL('./fixtures/master-mix-engine-stub.ts', import.meta.url));
const RUST_MODE_STUB = fileURLToPath(new URL('./fixtures/master-mix-rust-mode-stub.ts', import.meta.url));

// --------------------------------------------------------------------------
// the worklet, under a faked AudioWorkletGlobalScope
// --------------------------------------------------------------------------

let allocations = 0;
let Processor;

function installWorkletScope() {
	const RealInt16Array = Int16Array;
	// Count every `new Int16Array` the processor makes inside process(), the
	// audio-thread path (a returned buffer's view is made on a port message, not
	// per quantum); the shipped text looks
	// the global up at call time, so this sees exactly its allocations.
	const Counting = new Proxy(RealInt16Array, {
		construct(target, args) {
			if (globalThis.__countAllocations) allocations += 1;
			return Reflect.construct(target, args);
		}
	});
	globalThis.AudioWorkletProcessor = class {
		constructor() {
			this.__posts = [];
			this.port = {
				onmessage: null,
				postMessage: (message, transfer) => this.__posts.push({ message, transfer })
			};
		}
	};
	let registered = null;
	globalThis.registerProcessor = (name, ctor) => (registered = { name, ctor });
	// eslint-disable-next-line no-new-func -- the point is to run the shipped text
	new Function('Int16Array', readFrontendSource('src/lib/sets/master-capture-processor.js'))(Counting);
	assert.equal(registered?.name, 'mdt-master-capture', 'if it stops registering, every test below drives nothing');
	return registered.ctor;
}

function quantum(left, right = left) {
	return [[Float32Array.from(left), Float32Array.from(right)]];
}

function constant(value, frames = 128) {
	return new Array(frames).fill(value);
}

before(() => {
	Processor = installWorkletScope();
});

test('the worklet refuses a chunk size it cannot honour', () => {
	assert.throws(() => new Processor({ processorOptions: { chunkFrames: 64 } }), /chunkFrames >= 128/);
	assert.throws(() => new Processor({ processorOptions: {} }), /chunkFrames/);
});

test('the worklet writes interleaved int16 stereo, clamped, one transfer per full chunk', () => {
	const p = new Processor({ processorOptions: { chunkFrames: 256 } });
	assert.equal(p.process(quantum(constant(0.5), constant(-2))), true);
	assert.equal(p.__posts.length, 0, 'half a chunk must not post');
	p.process(quantum(constant(1), constant(Number.NaN)));
	assert.equal(p.__posts.length, 1);
	const { message, transfer } = p.__posts[0];
	assert.equal(message.frames, 256);
	assert.deepEqual(transfer, [message.pcm], 'the chunk is transferred, not copied');
	const pcm = new Int16Array(message.pcm);
	assert.deepEqual([pcm[0], pcm[1]], [16384, -32767]);
	assert.deepEqual([pcm[256], pcm[257]], [32767, 0]);
});

test('an idle master bus (no active input) is recorded as silence, so time keeps running', () => {
	const p = new Processor({ processorOptions: { chunkFrames: 128 } });
	p.process([[]]);
	p.process([]);
	assert.equal(p.__posts.length, 2);
	for (const { message } of p.__posts) {
		assert.equal(message.frames, 128);
		assert.ok(new Int16Array(message.pcm).every((v) => v === 0));
	}
});

test('steady state allocates nothing per quantum once the main thread returns buffers', () => {
	const p = new Processor({ processorOptions: { chunkFrames: 256 } });
	const run = (input) => {
		globalThis.__countAllocations = true;
		try {
			return p.process(input);
		} finally {
			globalThis.__countAllocations = false;
		}
	};
	const returnPosted = () => {
		while (p.__posts.length > 0) p.port.onmessage({ data: { reuse: p.__posts.pop().message.pcm } });
	};
	run(quantum(constant(0.1)));
	run(quantum(constant(0.1)));
	returnPosted();
	const before = allocations;
	for (let i = 0; i < 2000; i += 1) {
		run(quantum(constant(0.1)));
		returnPosted();
	}
	assert.equal(allocations - before, 0, 'a warm pool must cover every chunk');
	// Control: with nothing returned, the pool runs dry and it must allocate.
	const dry = new Processor({ processorOptions: { chunkFrames: 128 } });
	const beforeDry = allocations;
	globalThis.__countAllocations = true;
	dry.process(quantum(constant(0.1)));
	globalThis.__countAllocations = false;
	assert.equal(allocations - beforeDry, 1);
});

test('flush posts the partial chunk, then says so, then the processor ends', () => {
	const p = new Processor({ processorOptions: { chunkFrames: 1024 } });
	p.process(quantum(constant(0.25)));
	p.port.onmessage({ data: { flush: true } });
	assert.deepEqual(
		p.__posts.map(({ message }) => (message.flushed ? 'flushed' : message.frames)),
		[128, 'flushed']
	);
	assert.equal(p.process(quantum(constant(0.25))), false);
});

test('audio-thread cost of the tap is a small fraction of the 128-frame budget (measured)', () => {
	const p = new Processor({ processorOptions: { chunkFrames: 24000 } });
	const input = quantum(Array.from({ length: 128 }, (_, i) => Math.sin(i / 9) * 0.8));
	for (let i = 0; i < 2000; i += 1) p.process(input);
	const runs = 20000;
	const start = process.hrtime.bigint();
	for (let i = 0; i < runs; i += 1) {
		p.process(input);
		if (p.__posts.length > 0) p.port.onmessage({ data: { reuse: p.__posts.pop().message.pcm } });
	}
	const nsPerQuantum = Number(process.hrtime.bigint() - start) / runs;
	const budgetNs = (128 / 48000) * 1e9;
	console.log(
		`master-capture process(): ${(nsPerQuantum / 1000).toFixed(2)} us per 128-frame quantum, ` +
			`${((nsPerQuantum / budgetNs) * 100).toFixed(3)}% of the ${(budgetNs / 1000).toFixed(0)} us budget at 48 kHz`
	);
	assert.ok(nsPerQuantum < budgetNs * 0.05, `tap costs ${nsPerQuantum} ns per quantum`);
});

// --------------------------------------------------------------------------
// the main-thread capture: ordered posts, loud failure, clean stop
// --------------------------------------------------------------------------

let capture;

class FakePort {
	constructor() {
		this.onmessage = null;
		this.sent = [];
		this.listeners = [];
	}
	postMessage(message) {
		this.sent.push(message);
		if (message.flush) queueMicrotask(() => this.listeners.forEach((l) => l({ data: { flushed: true } })));
	}
	addEventListener(_type, listener) {
		this.listeners.push(listener);
	}
	start() {}
	emit(data) {
		this.onmessage?.({ data });
	}
}

function fakeGraph() {
	const worklets = [];
	const connections = [];
	const node = {
		connect: (to) => connections.push(['master', to]),
		disconnect: (to) => connections.push(['master-off', to])
	};
	const context = {
		state: 'running',
		sampleRate: 48000,
		destination: { name: 'destination' },
		audioWorklet: { addModule: async () => {} },
		resume: async () => {},
		createGain: () => ({ gain: { value: 1 }, connect: (to) => to, disconnect: () => {} })
	};
	globalThis.AudioWorkletNode = class {
		constructor(ctx, name, options) {
			this.name = name;
			this.options = options;
			this.port = new FakePort();
			worklets.push(this);
		}
		connect(to) {
			return to;
		}
		disconnect() {}
	};
	return { context, node, worklets, connections };
}

function chunk(frames) {
	return { pcm: new ArrayBuffer(frames * 4), frames };
}

async function settle() {
	for (let i = 0; i < 10; i += 1) await new Promise((r) => setTimeout(r, 0));
}

before(async () => {
	capture = await loadTypeScriptModule('tests/unit/fixtures/master-mix-capture-entry.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/rb/audio-engine.svelte': ENGINE_STUB, '$lib/audio-engine/rust-mode.svelte': RUST_MODE_STUB }
	});
});

test('chunks post in order, as raw PCM, to the session with stream, seq and rate', async () => {
	const graph = fakeGraph();
	capture.setMasterMixTapPoint({ context: graph.context, node: graph.node });
	const posts = [];
	const post = async (url, init) => {
		posts.push({ url, method: init.method, type: init.headers['content-type'], bytes: init.body.byteLength });
		return new Response(null, { status: 204 });
	};
	const handle = await capture.startMasterMixCapture('2026-10-06T03-00-00', assert.fail, post);
	const [worklet] = graph.worklets;
	assert.equal(worklet.name, 'mdt-master-capture');
	assert.equal(worklet.options.processorOptions.chunkFrames, 24000);
	assert.equal(worklet.options.channelCount, 2);
	assert.deepEqual(capture.builds.at(0), true, 'the first tap builds the graph if it has to');
	worklet.port.emit(chunk(24000));
	worklet.port.emit(chunk(24000));
	await settle();
	await handle.stop();
	const urls = posts.map((p) => new URL(p.url));
	assert.deepEqual(
		urls.map((u) => [u.origin + u.pathname, u.searchParams.get('seq'), u.searchParams.get('sample_rate')]),
		[
			[`${API_BASE}/api/sets/recorder/2026-10-06T03-00-00/master-pcm`, '0', '48000'],
			[`${API_BASE}/api/sets/recorder/2026-10-06T03-00-00/master-pcm`, '1', '48000']
		]
	);
	assert.equal(new Set(urls.map((u) => u.searchParams.get('stream'))).size, 1);
	assert.deepEqual(posts.map((p) => [p.method, p.type, p.bytes]), Array(2).fill(['POST', 'application/octet-stream', 96000]));
	// Each posted buffer goes back to the worklet's pool, and stop flushed it.
	assert.equal(worklet.port.sent.filter((m) => m.reuse).length, 2);
	assert.ok(worklet.port.sent.some((m) => m.flush === true));
});

test('a refused chunk stops the tap and says why, and nothing after it is posted', async () => {
	const graph = fakeGraph();
	capture.setMasterMixTapPoint({ context: graph.context, node: graph.node });
	const posted = [];
	const failures = [];
	const post = async (url) => {
		posted.push(new URL(url).searchParams.get('seq'));
		return posted.length === 1
			? new Response(null, { status: 204 })
			: new Response('chunk 1 arrived where 2 was expected', { status: 409 });
	};
	await capture.startMasterMixCapture('S', (why) => failures.push(why), post);
	const [worklet] = graph.worklets;
	worklet.port.emit(chunk(100));
	worklet.port.emit(chunk(100));
	await settle();
	worklet.port.emit(chunk(100));
	await settle();
	assert.deepEqual(posted, ['0', '1']);
	assert.equal(failures.length, 1);
	assert.match(failures[0], /refused master-mix audio \(409\): chunk 1/);
	assert.equal(worklet.port.onmessage, null, 'the failed tap is detached');
});

test('a backlog the daemon cannot drain fails the tap instead of buffering forever', async () => {
	const graph = fakeGraph();
	capture.setMasterMixTapPoint({ context: graph.context, node: graph.node });
	const failures = [];
	const post = () => new Promise(() => {});
	await capture.startMasterMixCapture('S', (why) => failures.push(why), post);
	for (let i = 0; i <= capture.MAX_QUEUED_CHUNKS + 1; i += 1) graph.worklets[0].port.emit(chunk(10));
	assert.equal(failures.length, 1);
	assert.match(failures[0], /fell .* behind the master mix/);
});

test('no master bus, or a context that will not run, rejects the start and attaches nothing', async () => {
	capture.setMasterMixTapPoint(null);
	await assert.rejects(capture.startMasterMixCapture('S', assert.fail, assert.fail), /no master bus/);
	const graph = fakeGraph();
	graph.context.state = 'suspended';
	capture.setMasterMixTapPoint({ context: graph.context, node: graph.node });
	await assert.rejects(capture.startMasterMixCapture('S', assert.fail, assert.fail), /will not run \(suspended\)/);
	assert.equal(graph.worklets.length, 0);
});

test('the chunk URL and size are what the daemon route expects', () => {
	assert.equal(capture.chunkFramesFor(44100), 22050);
	assert.equal(
		capture.masterPcmUrl('', '2026-10-06T03-00-00', 'a b', 3, 48000),
		'/api/sets/recorder/2026-10-06T03-00-00/master-pcm?stream=a+b&seq=3&sample_rate=48000'
	);
});
