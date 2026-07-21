import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let adapter;

before(async () => {
	adapter = await loadTypeScriptModule('src/lib/rb/stretch-adapter.ts');
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
		adapter.withStretchCommandTimeout(new Promise(() => {}), 'schedule', 5),
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
