import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { createServer } from 'vite';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let adapter;
let vite;

before(async () => {
	vite = await createServer({
		root: FRONTEND_ROOT,
		configFile: false,
		appType: 'custom',
		logLevel: 'silent',
		server: { middlewareMode: true }
	});
	adapter = await vite.ssrLoadModule('/src/lib/rb/stretch-adapter.ts');
});

after(async () => {
	await vite.close();
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
});

test('invalid schedule values fail before reaching the processor', () => {
	assert.throws(
		() => adapter.buildStretchSchedule(Number.NaN, { active: true }),
		/output time must be a finite non-negative number/
	);
	assert.throws(
		() => adapter.buildStretchSchedule(1, { rate: 0 }),
		/rate must be a finite positive number/
	);
	assert.throws(
		() => adapter.buildStretchSchedule(1, { loopStart: 2, loopEnd: 2 }),
		/loopStart must be less than loopEnd/
	);
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

test('processor errors become terminal typed failures', () => {
	const error = adapter.stretchProcessorError(new Event('processorerror'));
	assert.equal(error.name, 'StretchProcessorError');
	assert.match(error.message, /Signalsmith processor failed/);
});
