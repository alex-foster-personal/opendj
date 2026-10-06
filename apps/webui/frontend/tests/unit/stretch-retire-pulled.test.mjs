// W4 build-17 packaged verify, memory item 6a (MEM-6A here).
// A retired stretch processor must stay PULLED by a muted sink until its
// worklet has acknowledged dispose and a few render quanta have passed, because
// WebKit only drops an AudioWorkletNode's processor (and its WASM state) when
// process() returns false on a rendered quantum. Disconnecting first, the old
// order, left every retired processor resident until the context closed.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource as readSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let adapter;

before(async () => {
	adapter = await loadTypeScriptModule('src/lib/rb/stretch-adapter.ts');
});

/** An AudioContext whose clock runs in real time while `state` is running. */
function fakeContext({ state = 'running', sampleRate = 48_000 } = {}) {
	const startedAt = performance.now();
	let gainsCreated = 0;
	const destination = { name: 'destination', inputs: new Set() };
	const ctx = {
		state,
		sampleRate,
		destination,
		get currentTime() {
			return ctx.state === 'running' ? (performance.now() - startedAt) / 1000 : 0;
		},
		createGain() {
			gainsCreated += 1;
			const gain = {
				name: `gain${gainsCreated}`,
				gain: { value: 1 },
				inputs: new Set(),
				outputs: new Set(),
				connect(target) {
					gain.outputs.add(target);
					target.inputs.add(gain);
				}
			};
			return gain;
		},
		gainsCreated: () => gainsCreated
	};
	return ctx;
}

/** A stretch node that tracks its real connections and, like WebKit, only
 * counts as released if it was still pulled when the dispose ack landed AND
 * stayed pulled for at least three render quanta afterwards. */
function fakeNode(ctx, { dropAck = false } = {}) {
	const outputs = new Set();
	const events = [];
	let ackAt = null;
	let released = false;
	let portClosed = false;
	const node = {
		outputs,
		connect(target) {
			outputs.add(target);
			target.inputs.add(node);
			events.push(`connect:${target.name}`);
		},
		disconnect() {
			if (ackAt !== null && !released) {
				const pulledSec = ctx.currentTime - ackAt;
				const sinkPulled = [...outputs].some((o) => o.gain?.value === 0);
				if (sinkPulled && pulledSec >= (3 * 128) / ctx.sampleRate) released = true;
			}
			for (const target of outputs) target.inputs.delete(node);
			outputs.clear();
			events.push('disconnect');
		},
		addEventListener() {},
		removeEventListener() {},
		dispose() {
			events.push(`rpc:dispose(pulled=${[...outputs].some((o) => o.gain?.value === 0)})`);
			if (dropAck) return new Promise(() => {});
			ackAt = ctx.currentTime;
			return Promise.resolve();
		},
		port: {
			onmessage: null,
			close() {
				portClosed = true;
			}
		}
	};
	return { node, events, released: () => released, portClosed: () => portClosed };
}

function bus() {
	return { name: 'deck-bus', inputs: new Set() };
}

test('MEM-6A: dispose leaves the audible bus before it first yields, and only the muted sink pulls the node', async () => {
	const ctx = fakeContext();
	const fake = fakeNode(ctx);
	const deckBus = bus();
	const processor = new adapter.StretchDeckProcessor(ctx, fake.node, { onProcessorError() {} });
	processor.connect(deckBus);
	assert.ok(deckBus.inputs.has(fake.node), 'control: the live node feeds the deck bus');

	const disposing = processor.dispose();
	// Synchronous, before any await resolves.
	assert.equal(deckBus.inputs.size, 0, 'if a retiring node still feeds the deck bus then it can be heard - broken');
	const pulledBy = [...fake.node.outputs];
	assert.equal(pulledBy.length, 1);
	assert.equal(pulledBy[0].gain.value, 0, 'if the node is pulled by anything but a gain-0 sink then retirement is audible - broken');
	assert.ok(pulledBy[0].outputs.has(ctx.destination), 'the muted sink must be rendered (into the destination) or nothing pulls the node');

	await disposing;
	assert.equal(fake.node.outputs.size, 0, 'if a disposed node is left connected then it stays pulled for ever - broken');
	assert.ok(fake.portClosed());
});

test('MEM-6A: the dispose command lands while the node is still pulled, and it stays pulled for the release quanta', async () => {
	const ctx = fakeContext();
	const fake = fakeNode(ctx);
	const processor = new adapter.StretchDeckProcessor(ctx, fake.node, { onProcessorError() {} });
	processor.connect(bus());
	await processor.dispose();
	assert.ok(
		fake.events.includes('rpc:dispose(pulled=true)'),
		`if the node is disconnected before the dispose command then process() never runs again and WebKit never frees the processor - broken (${fake.events.join(' ')})`
	);
	assert.ok(fake.released(), 'if the node is disconnected before 3 render quanta pass after the ack then the processor is never released - broken');
});

test('MEM-6A: a dropped ack still ends with nothing on the sink, across repeated load/eject cycles', async () => {
	const ctx = fakeContext();
	const warnings = [];
	const realWarn = console.warn;
	console.warn = (line) => warnings.push(String(line));
	try {
		const cycles = await Promise.all(
			[0, 1, 2].map(async () => {
				const fake = fakeNode(ctx, { dropAck: true });
				const processor = new adapter.StretchDeckProcessor(ctx, fake.node, {
					commandTimeoutMs: 20,
					onProcessorError() {}
				});
				processor.connect(bus());
				await assert.rejects(processor.dispose(), { name: 'StretchCommandTimeoutError' });
				return fake;
			})
		);
		const sink = adapter.stretchRetirementSink(ctx);
		assert.equal(sink.inputs.size, 0, 'if a node whose ack never came stays on the sink then it is pulled for ever - broken');
		for (const fake of cycles) assert.ok(fake.portClosed());
		assert.equal(warnings.filter((l) => /release not confirmed/.test(l)).length, 3, 'each unconfirmed release logs exactly one line');
	} finally {
		console.warn = realWarn;
	}
});

test('MEM-6A: the pull is bounded even when the ack arrives but the context never renders', async () => {
	const ctx = fakeContext({ state: 'suspended' });
	const fake = fakeNode(ctx);
	const processor = new adapter.StretchDeckProcessor(ctx, fake.node, { onProcessorError() {} });
	const realWarn = console.warn;
	console.warn = () => {};
	const startedAt = performance.now();
	try {
		await processor.dispose();
	} finally {
		console.warn = realWarn;
	}
	assert.ok(performance.now() - startedAt < adapter.STRETCH_RETIRE_PULL_MAX_MS + 250);
	assert.equal(fake.node.outputs.size, 0);
});

test('MEM-6A: one muted sink per context, created once', async () => {
	const a = fakeContext();
	const b = fakeContext();
	for (let i = 0; i < 4; i++) {
		const fake = fakeNode(a);
		const processor = new adapter.StretchDeckProcessor(a, fake.node, { onProcessorError() {} });
		await processor.dispose();
	}
	assert.equal(a.gainsCreated(), 1, 'if every dispose makes its own sink then sinks accumulate - broken');
	assert.notEqual(adapter.stretchRetirementSink(a), adapter.stretchRetirementSink(b));
	assert.equal(b.gainsCreated(), 1);
	assert.equal(adapter.stretchRetirementSink(a).gain.value, 0);
});

test('MEM-6A: a closed context gets no sink and the dispose still completes', async () => {
	const ctx = fakeContext({ state: 'closed' });
	const fake = fakeNode(ctx);
	const processor = new adapter.StretchDeckProcessor(ctx, fake.node, { onProcessorError() {} });
	await processor.dispose();
	assert.equal(ctx.gainsCreated(), 0);
	assert.ok(fake.portClosed());
});

test('MEM-6A shape: only dispose() routes to the muted sink, so a live deck node never reaches it', () => {
	const src = readSource('src/lib/rb/stretch-adapter.ts');
	const uses = src.split('stretchRetirementSink(').length - 1;
	// one definition + one call, and the call sits inside dispose()
	assert.equal(uses, 2, 'if the sink is reachable from anywhere but dispose() then a playing deck could be muted into it - broken');
	const disposeAt = src.indexOf('async dispose(): Promise<void>');
	const callAt = src.indexOf('stretchRetirementSink(this.#context)');
	const nextMethodAt = src.indexOf('async load(', disposeAt);
	assert.ok(disposeAt > 0 && callAt > disposeAt && callAt < nextMethodAt);
	assert.ok(src.indexOf('this.#disposed = true;', disposeAt) < callAt, 'the sink is only reached after the processor is marked disposed');
});
