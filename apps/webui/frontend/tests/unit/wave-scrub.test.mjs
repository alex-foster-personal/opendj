import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let createLatestSeekDispatcher;
let waveClickTargetMs;
let waveDragTargetMs;

before(async () => {
	({ createLatestSeekDispatcher, waveClickTargetMs, waveDragTargetMs } =
		await loadTypeScriptModule('src/lib/components/rb/wave/wave-scrub.ts'));
});

test('waveform click seeks the engine to the track time beneath the pointer', () => {
	assert.equal(
		waveClickTargetMs({
			centerPositionMs: 60_000,
			pointerX: 300,
			widthPx: 400,
			durationMs: 180_000,
			windowSeconds: 24
		}),
		66_000
	);
});

test('dragging the scrolling waveform uses direct manipulation around a frozen engine position', () => {
	const common = {
		originPositionMs: 60_000,
		originClientX: 200,
		widthPx: 400,
		durationMs: 180_000,
		windowSeconds: 24
	};
	assert.equal(waveDragTargetMs({ ...common, clientX: 300 }), 54_000);
	assert.equal(waveDragTargetMs({ ...common, clientX: 100 }), 66_000);
});

test('waveform seek targets clamp to the real track boundaries and reject invalid geometry', () => {
	assert.equal(
		waveDragTargetMs({
			originPositionMs: 1_000,
			originClientX: 0,
			clientX: 400,
			widthPx: 400,
			durationMs: 180_000,
			windowSeconds: 24
		}),
		0
	);
	assert.equal(
		waveClickTargetMs({
			centerPositionMs: 179_000,
			pointerX: 400,
			widthPx: 400,
			durationMs: 180_000,
			windowSeconds: 24
		}),
		180_000
	);
	assert.throws(
		() =>
			waveDragTargetMs({
				originPositionMs: 1_000,
				originClientX: 0,
				clientX: 1,
				widthPx: 0,
				durationMs: 180_000,
				windowSeconds: 24
			}),
		/widthPx must be finite and positive/
	);
});

test('latest seek dispatcher never drops the release target while an earlier seek is in flight', async () => {
	const calls = [];
	const releases = [];
	const dispatcher = createLatestSeekDispatcher(async (positionMs) => {
		calls.push(positionMs);
		await new Promise((resolve) => releases.push(resolve));
	});

	const first = dispatcher.request(60_000);
	dispatcher.request(54_000);
	const release = dispatcher.request(48_000);
	assert.deepEqual(calls, [60_000]);

	releases.shift()();
	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(calls, [60_000, 48_000]);

	releases.shift()();
	await Promise.all([first, release]);
	assert.equal(dispatcher.pending, false);
});
