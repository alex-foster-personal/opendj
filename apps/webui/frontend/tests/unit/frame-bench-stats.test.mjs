import test from 'node:test';
import assert from 'node:assert/strict';
import { assertFourDecksPlaying, summarizeFrameBench } from '../live/frame-bench-stats.mjs';

test('if the acceptance deltas do not give p95 60, max 60, one long frame, refresh 16 then broken', () => {
	const row = summarizeFrameBench({
		deltas: [8, 16, 16, 17, 60, 16],
		idleDeltas: [16, 16, 16, 16],
		waveRowMs: 1,
		sampleMs: 10
	});
	assert.deepEqual(row, {
		frames: 6,
		interval_p50_ms: 16,
		interval_p95_ms: 60,
		interval_max_ms: 60,
		long_frames_over_50ms: 1,
		wave_row_share: 0.1,
		refresh_interval_ms: 16
	});
});

test('if a 50 ms frame counts as long or a 50.01 ms frame does not then broken', () => {
	const row = summarizeFrameBench({
		deltas: [50, 50.01, 16],
		idleDeltas: [16],
		waveRowMs: 0,
		sampleMs: 100
	});
	assert.equal(row.long_frames_over_50ms, 1);
});

test('if 3 playing decks do not throw the named error then broken', () => {
	const message = 'four-deck-frame-bench: 3 of 4 decks playing';
	assert.throws(
		() =>
			assertFourDecksPlaying({
				1: { playing: true },
				2: { playing: true },
				3: { playing: true },
				4: { playing: false }
			}),
		{ message }
	);
	assert.throws(
		() =>
			assertFourDecksPlaying({ 1: { playing: true }, 2: { playing: true }, 3: { playing: true } }),
		{ message }
	);
});

test('if four playing decks throw then broken', () => {
	assert.doesNotThrow(() => assertFourDecksPlaying([1, 2, 3, 4].map(() => ({ playing: true }))));
});

test('if empty deltas return numbers instead of a named error then broken', () => {
	assert.throws(
		() => summarizeFrameBench({ deltas: [], idleDeltas: [16], waveRowMs: 0, sampleMs: 10 }),
		/^Error: four-deck-frame-bench:/
	);
});
