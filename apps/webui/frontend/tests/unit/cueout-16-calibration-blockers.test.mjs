// requirement: CUEOUT-14 (cue/master alignment calibration)
// [if] the audio graph is not built [then] the refusal says so instead of blaming the output mode
// [if] the mode is two_outputs with a device but no graph [then] the graph is the only reason given
// [if] every precondition holds [then] no reason is given and calibration may start
// [if] both the mode and the device are wrong [then] both are named, not just the first
// [if] audio_graph_ready is not a boolean [then] it throws rather than guessing
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let cueAlign;

before(async () => {
	globalThis.window = { localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} } };
	cueAlign = await loadTypeScriptModule('src/lib/player/cue-align-policy.ts');
});

test('if the audio graph is missing then that is what the refusal says', () => {
	const reasons = cueAlign.calibrationBlockers({
		audio_graph_ready: false,
		output_mode: 'two_outputs',
		selected_output_device_id: 'cue-device-1'
	});
	assert.equal(reasons.length, 1, 'only the graph is wrong, so only the graph is named');
	assert.match(reasons[0], /audio graph/);
	assert.doesNotMatch(reasons[0], /two_outputs|device is selected/);
});

test('if everything is ready then nothing blocks calibration', () => {
	assert.deepEqual(
		cueAlign.calibrationBlockers({
			audio_graph_ready: true,
			output_mode: 'two_outputs',
			selected_output_device_id: 'cue-device-1'
		}),
		[]
	);
});

test('if the mode and the device are both wrong then both are named', () => {
	const reasons = cueAlign.calibrationBlockers({
		audio_graph_ready: true,
		output_mode: 'practice',
		selected_output_device_id: null
	});
	assert.equal(reasons.length, 2);
	assert.ok(reasons.some((reason) => /two_outputs/.test(reason)));
	assert.ok(reasons.some((reason) => /no headphone output device/.test(reason)));
});

test('if the graph readiness is not a boolean then it throws rather than guessing', () => {
	assert.throws(
		() =>
			cueAlign.calibrationBlockers({
				audio_graph_ready: 'yes',
				output_mode: 'two_outputs',
				selected_output_device_id: 'cue-device-1'
			}),
		TypeError
	);
});

test('the live effects factory refuses through this list, so its message names the real cause', async () => {
	const fs = await import('node:fs/promises');
	// The factory moved to cue-align-audio.ts; headphones.ts is where the old
	// message lived, so both are held to it.
	const factory = await fs.readFile(new URL('../../src/lib/player/cue-align-audio.ts', import.meta.url), 'utf8');
	const headphones = await fs.readFile(new URL('../../src/lib/player/headphones.ts', import.meta.url), 'utf8');
	assert.match(factory, /calibrationBlockers\(\{/, 'the engine must reuse the shared list');
	for (const source of [factory, headphones]) {
		assert.doesNotMatch(
			source,
			/needs two_outputs with a selected headphone output/,
			'the old message blamed the output mode for a missing audio graph'
		);
	}
});

test('the promised duration covers every capture a run can make, so a healthy run never looks hung', async () => {
	const latency = await loadTypeScriptModule('src/lib/player/cue-latency.ts');
	const perCaptureMs = latency.CUE_LATENCY_PREROLL_MS + latency.cueLatencyCaptureMs(cueAlign.CUE_ALIGN_MAX_LAG_MS);
	// Worst case a healthy run can reach: every gain rung on both buses while finding
	// the level, every measured probe retried once, and the verify pair plus its retry.
	const captures = 2 * latency.CUE_LATENCY_GAIN_STEPS.length + cueAlign.CUE_ALIGN_RUNS * 2 * 2 + 2 * 2;
	assert.ok(cueAlign.estimatedCalibrationSeconds() * 1000 >= captures * perCaptureMs - 500,
		'if the estimate misses level-find rungs or probe retries then a weak but healthy run overruns the promise and looks hung - broken');
});
