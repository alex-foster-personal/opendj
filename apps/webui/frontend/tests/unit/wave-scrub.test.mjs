import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let createLatestSeekDispatcher;
let waveClickTargetMs;
let waveDragTargetMs;
let snapWaveTargetMs;
let waveSnapModeFromModifiers;

// 4/4, 128 BPM: downbeats every ~1.875s (0, 1.875, 3.75, ...), every other
// real beat at ~0.469s spacing.
const SNAP_GRID = [
	{ n: 1, bpm: 128, t: 0.0 },
	{ n: 2, bpm: 128, t: 0.469 },
	{ n: 3, bpm: 128, t: 0.938 },
	{ n: 4, bpm: 128, t: 1.406 },
	{ n: 1, bpm: 128, t: 1.875 },
	{ n: 2, bpm: 128, t: 2.344 },
	{ n: 3, bpm: 128, t: 2.813 },
	{ n: 4, bpm: 128, t: 3.281 },
	{ n: 1, bpm: 128, t: 3.75 }
];

before(async () => {
	({ createLatestSeekDispatcher, waveClickTargetMs, waveDragTargetMs, snapWaveTargetMs, waveSnapModeFromModifiers } =
		await loadTypeScriptModule('src/lib/components/rb/wave/wave-scrub.ts'));
});

// --------------------------------------------------------- snap-to-downbeat

test('waveSnapModeFromModifiers: no modifier is downbeat, shift alone is exact, cmd+shift is beat', () => {
	assert.equal(waveSnapModeFromModifiers({ shiftKey: false, metaKey: false }), 'downbeat');
	assert.equal(waveSnapModeFromModifiers({ shiftKey: false, metaKey: true }), 'downbeat');
	assert.equal(waveSnapModeFromModifiers({ shiftKey: true, metaKey: false }), 'exact');
	assert.equal(waveSnapModeFromModifiers({ shiftKey: true, metaKey: true }), 'beat');
});

test('snapWaveTargetMs default (downbeat) snaps a scrub target to the nearest bar downbeat', () => {
	// 1.0s is nearer downbeat 1.875 than 0.0? distance 1.0 vs 0.875 -> nearest is 1.875.
	assert.equal(snapWaveTargetMs(1000, SNAP_GRID, 'downbeat'), 1875);
	// 0.3s is nearer downbeat 0.0 than 1.875.
	assert.equal(snapWaveTargetMs(300, SNAP_GRID, 'downbeat'), 0);
});

test('snapWaveTargetMs "exact" (Shift held) never snaps, returning the raw target unchanged', () => {
	assert.equal(snapWaveTargetMs(1000, SNAP_GRID, 'exact'), 1000);
	assert.equal(snapWaveTargetMs(1000, null, 'exact'), 1000);
});

test('snapWaveTargetMs "beat" (Cmd+Shift held) snaps to any real beat, not only a downbeat', () => {
	// 0.5s is nearer the n=2 beat at 0.469 than the downbeat at 0.0 or 1.875.
	assert.equal(snapWaveTargetMs(500, SNAP_GRID, 'beat'), 469);
	assert.equal(snapWaveTargetMs(500, SNAP_GRID, 'downbeat'), 0);
});

test('snapWaveTargetMs falls back to the raw target rather than throwing when no usable beatgrid exists', () => {
	assert.equal(snapWaveTargetMs(1000, null, 'downbeat'), 1000);
	assert.equal(snapWaveTargetMs(1000, undefined, 'downbeat'), 1000);
	assert.equal(snapWaveTargetMs(1000, [], 'downbeat'), 1000, 'an empty grid fails validateBeatGrid gracefully');
	// No n===1 downbeat at all in this (otherwise valid) grid.
	const noDownbeat = [
		{ n: 2, bpm: 128, t: 0.1 },
		{ n: 3, bpm: 128, t: 0.57 }
	];
	assert.equal(snapWaveTargetMs(1000, noDownbeat, 'downbeat'), 1000);
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
