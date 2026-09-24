// requirement: CUEOUT-19
// [if] cue-bridge-processor.js drifts from cue-bridge-ring.ts [then] the worklet and the unit tests disagree on buffering policy
// [if] priming, steady-state target, drift correction, underrun repriming, or stereo preservation diverge [then] the bridge misbehaves in the browser only the ring tests would catch
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const QUANTUM = 128;
const PROCESSOR_PATH = fileURLToPath(
	new URL('../../src/lib/player/cue-bridge-processor.js', import.meta.url)
);

let ringMod;
let SenderProcessor;
let ReceiverProcessor;

function stereoChunk(valueL, valueR, frames = QUANTUM) {
	return {
		left: new Float32Array(frames).fill(valueL),
		right: new Float32Array(frames).fill(valueR)
	};
}

function loadProcessorClasses() {
	const registered = {};
	class StubAudioWorkletProcessor {
		constructor() {
			this.port = {
				onmessage: null,
				postMessage(data, _transfer) {
					if (typeof this.onmessage === 'function') {
						this.onmessage({ data });
					}
				}
			};
		}
	}
	const prior = {
		AudioWorkletProcessor: globalThis.AudioWorkletProcessor,
		registerProcessor: globalThis.registerProcessor
	};
	globalThis.AudioWorkletProcessor = StubAudioWorkletProcessor;
	globalThis.registerProcessor = (name, cls) => {
		registered[name] = cls;
	};
	const source = readFileSync(PROCESSOR_PATH, 'utf8');
	// eslint-disable-next-line no-new-func
	new Function(source)();
	globalThis.AudioWorkletProcessor = prior.AudioWorkletProcessor;
	globalThis.registerProcessor = prior.registerProcessor;
	return { SenderProcessor: registered['cue-bridge-sender'], ReceiverProcessor: registered['cue-bridge-receiver'] };
}

/**
 * A real `node:worker_threads` MessageChannel delivers `onmessage` on a later
 * event-loop tick, same as the DOM one across a worklet boundary - correct for
 * the real bridge, but every test here drives push/pull back to back in one
 * synchronous script with nothing to yield to, so a real port would leave
 * every "push then immediately pull" loop spinning on a fill that can only
 * ever change on a tick this code never returns control for. This pair
 * delivers synchronously instead, which is exactly what two processors
 * scheduled on their own separate audio-thread callbacks are not, but the
 * pull/push policy under test does not know or care when its port fires -
 * only that `postMessage` in implies `onmessage` out with the same payload.
 */
function createSyncPortPair() {
	const portA = { onmessage: null, postMessage: null };
	const portB = { onmessage: null, postMessage: null };
	portA.postMessage = (data) => {
		if (typeof portB.onmessage === 'function') portB.onmessage({ data });
	};
	portB.postMessage = (data) => {
		if (typeof portA.onmessage === 'function') portA.onmessage({ data });
	};
	return [portA, portB];
}

function wirePortBridge(sender, receiver, capacity) {
	const [port1, port2] = createSyncPortPair();
	sender.port.postMessage({ type: 'connect', port: port1 });
	receiver.port.postMessage({ type: 'connect', port: port2 });
	return { sender, receiver, capacity };
}

function createPortBridge(capacity) {
	const sender = new SenderProcessor({ processorOptions: { mode: 'port' } });
	const receiver = new ReceiverProcessor({ processorOptions: { mode: 'port', capacity } });
	return wirePortBridge(sender, receiver, capacity);
}

function ringPush(ring, left, right) {
	ring.push(left, right);
}

function ringPull(ring, frames = QUANTUM) {
	const left = new Float32Array(frames);
	const right = new Float32Array(frames);
	const result = ring.pull(left, right);
	return { left, right, result, fill: ring.fill, priming: ring.priming };
}

function processorPush(sender, left, right) {
	sender.process([[left, right]], []);
}

function processorPull(receiver, frames = QUANTUM) {
	const left = new Float32Array(frames);
	const right = new Float32Array(frames);
	receiver.process([], [[left, right]]);
	// `process()` itself must return a plain boolean per the Web Audio spec, so
	// the per-call result is stashed on the instance as `_lastPull` (see
	// cue-bridge-processor.js); `result.priming` there is the CueBridgeRing.pull()-
	// shaped "was this call itself a priming call" flag, distinct from
	// `receiver._priming`, which is the state the call left behind.
	return { left, right, result: receiver._lastPull, fill: receiver.fill, priming: receiver._priming };
}

function primeRingToTarget(ring, target) {
	const chunk = stereoChunk(0.2, -0.2);
	while (ring.fill < target) {
		ringPush(ring, chunk.left, chunk.right);
	}
	while (ring.priming) {
		ringPull(ring);
	}
}

function primeProcessorToTarget(sender, receiver, target, capacity) {
	const chunk = stereoChunk(0.2, -0.2);
	while (receiver.fill < target) {
		processorPush(sender, chunk.left, chunk.right);
	}
	while (receiver._priming) {
		processorPull(receiver);
	}
}

before(async () => {
	ringMod = await loadTypeScriptModule('src/lib/player/cue-bridge-ring.ts');
	({ SenderProcessor, ReceiverProcessor } = loadProcessorClasses());
});

test('priming to target matches between ring and processor', () => {
	const ring = new ringMod.CueBridgeRing();
	const { sender, receiver } = createPortBridge(ringMod.CUE_BRIDGE_RING_CAPACITY);
	const chunk = stereoChunk(0.4, -0.4);

	while (ring.fill < ringMod.CUE_BRIDGE_TARGET_FRAMES) {
		ringPush(ring, chunk.left, chunk.right);
		processorPush(sender, chunk.left, chunk.right);
	}
	while (ring.priming || receiver._priming) {
		const ringOut = ringPull(ring);
		const procOut = processorPull(receiver);
		assert.equal(ringOut.result.priming, true);
		assert.equal(procOut.result.priming, true);
		assert.ok(ringOut.left.every((sample) => sample === 0));
		assert.ok(procOut.left.every((sample) => sample === 0));
	}
	assert.equal(ring.fill, ringMod.CUE_BRIDGE_TARGET_FRAMES);
	assert.equal(receiver.fill, ringMod.CUE_BRIDGE_TARGET_FRAMES);
});

test('steady state holds the target fill exactly in ring and processor', () => {
	const ring = new ringMod.CueBridgeRing();
	const { sender, receiver } = createPortBridge(ringMod.CUE_BRIDGE_RING_CAPACITY);
	primeRingToTarget(ring, ringMod.CUE_BRIDGE_TARGET_FRAMES);
	primeProcessorToTarget(sender, receiver, ringMod.CUE_BRIDGE_TARGET_FRAMES, ringMod.CUE_BRIDGE_RING_CAPACITY);
	const chunk = stereoChunk(0.5, -0.5);
	for (let i = 0; i < 24; i += 1) {
		ringPush(ring, chunk.left, chunk.right);
		ringPull(ring);
		processorPush(sender, chunk.left, chunk.right);
		processorPull(receiver);
	}
	assert.equal(ring.fill, ringMod.CUE_BRIDGE_TARGET_FRAMES);
	assert.equal(receiver.fill, ringMod.CUE_BRIDGE_TARGET_FRAMES);
});

test('drop-above-band matches between ring and processor', () => {
	const ring = new ringMod.CueBridgeRing();
	const { sender, receiver } = createPortBridge(ringMod.CUE_BRIDGE_RING_CAPACITY);
	primeRingToTarget(ring, ringMod.CUE_BRIDGE_TARGET_FRAMES);
	primeProcessorToTarget(sender, receiver, ringMod.CUE_BRIDGE_TARGET_FRAMES, ringMod.CUE_BRIDGE_RING_CAPACITY);
	// Two real-sized pushes, not one double-length one: a real process() call
	// always hands the sender exactly one render quantum (the sender's scratch
	// buffer is sized for that), so a 256-frame single push is not a shape the
	// processor is ever actually driven with.
	const extra = stereoChunk(0.1, -0.1, QUANTUM);
	ringPush(ring, extra.left, extra.right);
	ringPush(ring, extra.left, extra.right);
	processorPush(sender, extra.left, extra.right);
	processorPush(sender, extra.left, extra.right);
	const ringBefore = ring.fill;
	const procBefore = receiver.fill;
	ringPull(ring);
	processorPull(receiver);
	assert.equal(ring.fill, ringBefore - QUANTUM - 1);
	assert.equal(receiver.fill, procBefore - QUANTUM - 1);
});

test('duplicate-below-band matches between ring and processor', () => {
	const ring = new ringMod.CueBridgeRing();
	const { sender, receiver } = createPortBridge(ringMod.CUE_BRIDGE_RING_CAPACITY);
	primeRingToTarget(ring, ringMod.CUE_BRIDGE_TARGET_FRAMES);
	primeProcessorToTarget(sender, receiver, ringMod.CUE_BRIDGE_TARGET_FRAMES, ringMod.CUE_BRIDGE_RING_CAPACITY);
	while (ring.fill >= ringMod.CUE_BRIDGE_TARGET_FRAMES - ringMod.CUE_BRIDGE_DRIFT_BAND_FRAMES) {
		ringPull(ring);
		processorPull(receiver);
	}
	while (receiver.fill >= ringMod.CUE_BRIDGE_TARGET_FRAMES - ringMod.CUE_BRIDGE_DRIFT_BAND_FRAMES) {
		processorPull(receiver);
	}
	const ringBefore = ring.fill;
	const procBefore = receiver.fill;
	const ringOut = ringPull(ring);
	const procOut = processorPull(receiver);
	assert.equal(ringOut.result.duplicated, true);
	assert.notEqual(ringOut.left[0], 0);
	assert.notEqual(procOut.left[0], 0);
	assert.equal(ringOut.left[1], ringOut.left[0]);
	assert.equal(procOut.left[1], procOut.left[0]);
	assert.equal(ring.fill, ringBefore - (QUANTUM - 1));
	assert.equal(receiver.fill, procBefore - (QUANTUM - 1));
});

test('underrun repriming matches between ring and processor', () => {
	const ring = new ringMod.CueBridgeRing();
	const { sender, receiver } = createPortBridge(ringMod.CUE_BRIDGE_RING_CAPACITY);
	primeRingToTarget(ring, ringMod.CUE_BRIDGE_TARGET_FRAMES);
	primeProcessorToTarget(sender, receiver, ringMod.CUE_BRIDGE_TARGET_FRAMES, ringMod.CUE_BRIDGE_RING_CAPACITY);
	while (ring.fill > 0) ringPull(ring);
	while (receiver.fill > 0) processorPull(receiver);
	// The drain above stops the instant fill reads 0, which can land exactly on
	// a quantum boundary without ever handing either side a pull while it is
	// ALREADY empty - that is the call that actually flips it back to priming.
	// One more pull each forces that call.
	ringPull(ring);
	processorPull(receiver);
	assert.equal(ring.priming, true);
	assert.equal(receiver._priming, true);
	const chunk = stereoChunk(0.3, -0.3);
	while (ring.priming) {
		const before = ring.fill;
		const out = ringPull(ring);
		assert.equal(out.result.priming, true);
		assert.equal(ring.fill, before);
		if (ring.priming) ringPush(ring, chunk.left, chunk.right);
	}
	while (receiver._priming) {
		const before = receiver.fill;
		const out = processorPull(receiver);
		assert.equal(out.result.priming, true);
		assert.equal(receiver.fill, before);
		if (receiver._priming) processorPush(sender, chunk.left, chunk.right);
	}
	assert.equal(ring.fill, ringMod.CUE_BRIDGE_TARGET_FRAMES);
	assert.equal(receiver.fill, ringMod.CUE_BRIDGE_TARGET_FRAMES);
});

test('stereo preservation matches between ring and processor', () => {
	const ring = new ringMod.CueBridgeRing();
	const { sender, receiver } = createPortBridge(ringMod.CUE_BRIDGE_RING_CAPACITY);
	// Exact in float32 (both are terminating binary fractions), unlike 0.6/-0.4:
	// every buffer on this path is a Float32Array, so a value that only
	// round-trips in float64 would fail the comparison on precision alone,
	// not on a real bridge bug.
	const leftIn = new Float32Array([0.625]);
	const rightIn = new Float32Array([-0.375]);
	while (ring.fill < ringMod.CUE_BRIDGE_TARGET_FRAMES) {
		ringPush(ring, leftIn, rightIn);
		processorPush(sender, leftIn, rightIn);
	}
	while (ring.priming) ringPull(ring);
	while (receiver._priming) processorPull(receiver);
	const ringOut = ringPull(ring, 1);
	const procOut = processorPull(receiver, 1);
	assert.equal(ringOut.left[0], 0.625);
	assert.equal(ringOut.right[0], -0.375);
	assert.equal(procOut.left[0], 0.625);
	assert.equal(procOut.right[0], -0.375);
});

test('SAB push overflow does not advance the receiver read cursor', () => {
	const capacity = ringMod.CUE_BRIDGE_RING_CAPACITY;
	const sab = new SharedArrayBuffer(16 + capacity * 2 * 4);
	const control = new Int32Array(sab, 0, 4);
	control[0] = 0;
	control[1] = 0;
	control[2] = 0;
	control[3] = 0;
	const sender = new SenderProcessor({ processorOptions: { mode: 'sab', controlBuffer: sab } });
	const receiver = new ReceiverProcessor({ processorOptions: { mode: 'sab', controlBuffer: sab } });
	const chunk = stereoChunk(0.9, -0.9, capacity + QUANTUM);
	sender.process([[chunk.left, chunk.right]], []);
	const readBefore = Atomics.load(control, 1);
	const overflow = Atomics.load(control, 3);
	processorPull(receiver);
	const readAfter = Atomics.load(control, 1);
	assert.equal(readBefore, readAfter, 'sender overflow must not mutate SAB_READ');
	assert.ok(overflow > 0, 'overflow counter must record dropped push frames');
});
