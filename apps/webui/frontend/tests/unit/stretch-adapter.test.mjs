import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource as readSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let adapter;
let stretchErrors;

before(async () => {
	adapter = await loadTypeScriptModule('src/lib/rb/stretch-adapter.ts');
	stretchErrors = await loadTypeScriptModule('src/lib/rb/stretch-errors.ts');
});

test('stretch node keeps one disconnected input and a fixed stereo output', () => {
	assert.deepEqual(adapter.STRETCH_NODE_OPTIONS, {
		numberOfInputs: 1,
		numberOfOutputs: 1,
		outputChannelCount: [2],
		channelCount: 2,
		channelCountMode: 'explicit',
		channelInterpretation: 'speakers'
	});
});

test('schedule duplicates the package output field as outputTime', () => {
	assert.deepEqual(
		adapter.buildStretchSchedule(12.25, {
			active: true,
			input: 3.5,
			rate: 1.04,
			semitones: 0,
			loopStart: 8,
			loopEnd: 16
		}),
		{
			output: 12.25,
			outputTime: 12.25,
			active: true,
			input: 3.5,
			rate: 1.04,
			semitones: 0,
			loopStart: 8,
			loopEnd: 16
		}
	);
	assert.throws(
		() => adapter.buildStretchSchedule(4, { output: 99, outputTime: 100, active: false }),
		/unknown Signalsmith schedule field/
	);
});

test('invalid schedule values fail before reaching the processor', () => {
	assert.deepEqual(adapter.buildStretchSchedule(1, { loopStart: 0, loopEnd: 0 }), {
		output: 1,
		outputTime: 1,
		loopStart: 0,
		loopEnd: 0
	});
	assert.throws(
		() => adapter.buildStretchSchedule(Number.NaN, { active: true }),
		/output time must be a finite non-negative number/
	);
	assert.throws(
		() => adapter.buildStretchSchedule(1, { rate: 0 }),
		/rate must be a finite positive number/
	);
	assert.throws(
		() => adapter.buildStretchSchedule(1, { loopStart: 3, loopEnd: 2 }),
		/loopStart must not exceed loopEnd/
	);
	assert.deepEqual(adapter.buildStretchSchedule(1, { loopStart: 2, loopEnd: 2 }), {
		output: 1,
		outputTime: 1,
		loopStart: 2,
		loopEnd: 2
	});
});

test('processor command timeout rejects explicitly', async () => {
	await assert.rejects(
		stretchErrors.withStretchCommandTimeout(new Promise(() => {}), 'schedule', 5),
		(error) => {
			assert.equal(error.name, 'StretchCommandTimeoutError');
			assert.match(error.message, /schedule timed out after 5ms/);
			return true;
		}
	);
});

test('timed-out processor is poisoned before a second command can post', async () => {
	const gate = new adapter.StretchCommandGate();
	let posts = 0;
	await assert.rejects(
		gate.run(
			'first command',
			() => {
				posts += 1;
				return new Promise(() => {});
			},
			5
		),
		/first command timed out/
	);
	await assert.rejects(
		gate.run('second command', async () => {
			posts += 1;
		}),
		/first command timed out/
	);
	assert.equal(posts, 1);
});

test('reload reset and buffer bounds are explicit and deterministic', () => {
	assert.deepEqual(adapter.STRETCH_RESET_CHANGE, {
		active: false,
		input: 0,
		rate: 1,
		semitones: 0,
		loopStart: 0,
		loopEnd: 0
	});
	assert.doesNotThrow(() =>
		adapter.validateStretchBufferMetadata(
			{ duration: 60, numberOfChannels: 2, sampleRate: 48000 },
			48000
		)
	);
	assert.throws(
		() => adapter.assertStretchLoadIsFresh(60),
		/one-shot/
	);
	assert.doesNotThrow(() => adapter.assertStretchLoadIsFresh(0));
	assert.throws(
		() =>
			adapter.validateStretchBufferMetadata(
				{ duration: 60, numberOfChannels: 2, sampleRate: 44100 },
				48000
			),
		/sample rate/
	);
	assert.throws(
		() => adapter.validateStretchScheduleBounds({ active: true }, 0, 0, 48000),
		/before audio buffers are loaded/
	);
	assert.throws(
		() => adapter.validateStretchScheduleBounds({ input: 61 }, 60, 48000, 48000),
		/outside loaded audio/
	);
	assert.throws(
		() => adapter.validateStretchScheduleBounds({ loopEnd: 61 }, 60, 48000, 48000),
		/exceeds loaded audio/
	);
});

test('processor errors become terminal typed failures', () => {
	const error = adapter.stretchProcessorError(new Event('processorerror'));
	assert.equal(error.name, 'StretchProcessorError');
	assert.match(error.message, /Signalsmith processor failed/);
});

function fakeStretchNode({ disposeImpl } = {}) {
	let processorErrorListener;
	let scheduleCalls = 0;
	let disposeCalls = 0;
	let portClosed = false;
	return {
		node: {
			addEventListener(type, listener) {
				if (type === 'processorerror') processorErrorListener = listener;
			},
			removeEventListener() {},
			connect() {},
			disconnect() {},
			schedule() {
				scheduleCalls += 1;
				return new Promise(() => {});
			},
			dispose() {
				disposeCalls += 1;
				return disposeImpl ? disposeImpl() : Promise.resolve();
			},
			port: {
				onmessage: null,
				close() {
					portClosed = true;
				}
			}
		},
		processorError() {
			assert.ok(processorErrorListener);
			processorErrorListener(new Event('processorerror'));
		},
		scheduleCalls() {
			return scheduleCalls;
		},
		disposeCalls() {
			return disposeCalls;
		},
		portClosed() {
			return portClosed;
		}
	};
}

test('terminal processor failure prevents later worklet invocation', async () => {
	const fake = fakeStretchNode();
	const processor = new adapter.StretchDeckProcessor(fakeAudioContext(), fake.node, {
		onProcessorError() {}
	});
	fake.processorError();

	await assert.rejects(processor.schedule(1, { active: true }), {
		name: 'StretchProcessorError'
	});
	assert.equal(fake.scheduleCalls(), 0);
});

test('command timeout is terminal and prevents a second worklet invocation', async () => {
	const fake = fakeStretchNode();
	const processor = new adapter.StretchDeckProcessor(fakeAudioContext(), fake.node, {
		commandTimeoutMs: 5,
		onProcessorError() {}
	});

	await assert.rejects(processor.schedule(1, { active: false }), {
		name: 'StretchCommandTimeoutError'
	});
	await assert.rejects(processor.schedule(2, { active: false }), {
		name: 'StretchCommandTimeoutError'
	});
	assert.equal(fake.scheduleCalls(), 1);
});

test('dispose disconnects synchronously, retires the worklet through a real command, and releases the port', async () => {
	const fake = fakeStretchNode();
	const processor = new adapter.StretchDeckProcessor(fakeAudioContext(), fake.node, {
		onProcessorError() {}
	});

	await processor.dispose();

	assert.equal(
		fake.disposeCalls(),
		1,
		'if dispose() never invokes the worklet node then the patched Signalsmith RPC that retires it and transfers back retained PCM never runs - broken'
	);
	assert.ok(
		fake.portClosed(),
		'if the MessagePort is never closed then a disposed deck keeps leaking it - broken'
	);

	// Idempotent: a second dispose() must not re-issue the worklet command.
	await processor.dispose();
	assert.equal(fake.disposeCalls(), 1);

	// The real #assertOperational guard, not a stand-in for it: a disposed
	// processor must refuse further commands.
	await assert.rejects(processor.schedule(1, { active: true }), /disposed/);
});

test('dispose still releases the port when the worklet dispose command times out', async () => {
	const fake = fakeStretchNode({ disposeImpl: () => new Promise(() => {}) });
	const processor = new adapter.StretchDeckProcessor(fakeAudioContext(), fake.node, {
		commandTimeoutMs: 5,
		onProcessorError() {}
	});

	await assert.rejects(processor.dispose(), { name: 'StretchCommandTimeoutError' });

	assert.equal(fake.disposeCalls(), 1);
	assert.ok(
		fake.portClosed(),
		'if a stalled worklet dispose command still leaves the port open then a hung Signalsmith RPC leaks a MessagePort and the real AudioWorklet forever - broken'
	);
});

test('create awaits ensureStretchWorkletReady before the handshake factory call', () => {
	const adapter = readSource('src/lib/rb/stretch-adapter.ts');
	const createAt = adapter.indexOf('static async create(');
	assert.ok(createAt !== -1);
	const createBody = adapter.slice(createAt, adapter.indexOf('\n\tconnect(', createAt));
	assert.ok(
		createBody.includes('ensureStretchWorkletReady'),
		'create must wait on module readiness before starting the handshake timer'
	);
	const readyAt = createBody.indexOf('ensureStretchWorkletReady');
	const handshakeAt = createBody.indexOf("'worklet ready handshake'");
	assert.ok(readyAt !== -1 && handshakeAt !== -1 && readyAt < handshakeAt);
	assert.ok(createBody.includes("'worklet ready handshake'"));
	assert.ok(!createBody.includes('processor creation timed out'));
	assert.ok(!readSource('src/lib/rb/stretch-worklet-ready.ts').includes('processor creation timed out'));
	assert.ok(createBody.includes("recordWorkletAck('processor creation'"));
});

/** A running AudioContext stand-in with what StretchDeckProcessor.dispose() needs. */
function fakeAudioContext() {
	const startedAt = performance.now();
	return {
		state: "running",
		sampleRate: 48_000,
		destination: { inputs: new Set() },
		get currentTime() {
			return (performance.now() - startedAt) / 1000;
		},
		createGain() {
			return { gain: { value: 1 }, inputs: new Set(), connect() {} };
		}
	};
}
