// [RESCUE-02] [RESCUE-03]
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let math;

before(async () => {
	math = await loadTypeScriptModule('src/lib/rb/performance-rescue-math.ts');
});

const GRID_MS = Array.from({ length: 128 }, (_, index) => index * (60_000 / 128));

const BEATGRID_STAMP = {
	kind: 'beatgrid',
	beat_index: 32,
	beat_n: 1,
	phase: 0
};

function _gigSnapshot(capturedAtMs) {
	return {
		schema: 1,
		captured_at_ms: capturedAtMs,
		reason: 'transport',
		app_posture: 'gig',
		master_deck: null,
		playlist_id: null,
		decks: {},
		mixer: {
			crossfader: 0.5,
			master: 0.5,
			headphones: {
				mix: 0,
				level: 1,
				output_mode: 'practice',
				selected_master_output_device_id: null,
				selected_output_device_id: null
			}
		}
	};
}

test('[RESCUE-02] elapsed 90s advances beat stamp at pitch 1.0 and 1.1', () => {
	const atPitch1 = math.advanceBeatStamp(BEATGRID_STAMP, GRID_MS, 1, 90_000);
	assert.ok(Math.abs(atPitch1 - (32 + 90 * (128 / 60))) < 1e-6);
	const pitched = math.advanceBeatStamp(BEATGRID_STAMP, GRID_MS, 1.1, 90_000);
	assert.ok(Math.abs(pitched - (32 + 90 * (128 / 60) * 1.1)) < 1e-6);
});

test('[RESCUE-02] missing beatgrid marks resume target as exception', () => {
	const result = math.computeResumeTarget(1, BEATGRID_STAMP, [], 1, 1_000);
	assert.equal('reason' in result, true);
});

test('[RESCUE-02] snapshot age 11 min is not playback eligible', () => {
	const snapshot = _gigSnapshot(0);
	assert.equal(math.playbackEligible(snapshot, 11 * 60 * 1000), false);
});

test('[RESCUE-02] gig snapshot stays eligible regardless of live prep posture helper', () => {
	const snapshot = _gigSnapshot(Date.now() - 60_000);
	assert.equal(math.gigPlaybackEligible(snapshot, Date.now()), true);
});

test('[RESCUE-03] decode wait helper only accepts decoded decks', () => {
	assert.equal(
		math.deckDecodedForRescue({
			stable_id: 'a',
			expected_stable_id: 'a',
			duration_ms: 120_000,
			processor_error: null
		}),
		true
	);
	assert.equal(
		math.deckDecodedForRescue({
			stable_id: 'a',
			expected_stable_id: 'b',
			duration_ms: 120_000,
			processor_error: null
		}),
		false
	);
});
