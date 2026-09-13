// requirement: CUEOUT-11 (slow)
// Adversarial evals: noisy capture, off-grid delay, long window, weak-then-strong.
// [if] a 247 ms lag is buried in noise at 0.25 rms [then] measureCueLatencyMs still returns 247
// [if] the same capture is noise-only at 0.8 rms [then] it throws instead of a plausible delay
// [if] lag is 37.4 ms (off integer ms) [then] the Mixxx field rounds to 37
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { addGaussianNoise, delayedCapture } from '../cue-latency-eval.mjs';
import { loadTypeScriptModule } from '../load-typescript.mjs';

const SAMPLE_RATE = 48000;

const cueLatency = await loadTypeScriptModule('src/lib/player/cue-latency.ts');

function captureAt(reference, delayMs, noiseRms, seed) {
	const delayed = delayedCapture(reference, delayMs, SAMPLE_RATE, 80);
	return noiseRms > 0 ? addGaussianNoise(delayed, noiseRms, seed) : delayed;
}

test('slow: 247 ms Bluetooth-class lag survives 0.25 rms noise', async () => {
	const measured = await cueLatency.measureCueLatencyMs({
		sampleRate: SAMPLE_RATE,
		clickCount: 8,
		periodMs: 70,
		playAndRecord: async (reference) => captureAt(reference, 247, 0.25, 13)
	});
	assert.equal(measured, 247);
});

test('slow: 37.4 ms off-grid lag rounds to Mixxx integer 37', async () => {
	const measured = await cueLatency.measureCueLatencyMs({
		sampleRate: SAMPLE_RATE,
		playAndRecord: async (reference) => captureAt(reference, 37.4, 0.05, 3)
	});
	assert.equal(measured, 37);
});

test('slow: 120 ms lag across three noise seeds stays 120', async () => {
	for (const seed of [1, 2, 99]) {
		const measured = await cueLatency.measureCueLatencyMs({
			sampleRate: SAMPLE_RATE,
			clickCount: 6,
			playAndRecord: async (reference) => captureAt(reference, 120, 0.15, seed)
		});
		assert.equal(measured, 120, `seed ${seed} drifted to ${measured}`);
	}
});

test('slow: 500 ms ceiling lag survives 0.2 rms noise', async () => {
	const measured = await cueLatency.measureCueLatencyMs({
		sampleRate: SAMPLE_RATE,
		clickCount: 8,
		periodMs: 70,
		playAndRecord: async (reference) => captureAt(reference, 500, 0.2, 21)
	});
	assert.equal(measured, 500);
});

test('slow: loud noise-only capture does not invent a 0-500 ms delay', async () => {
	await assert.rejects(
		() =>
			cueLatency.measureCueLatencyMs({
				sampleRate: SAMPLE_RATE,
				clickCount: 8,
				playAndRecord: async (reference) =>
					addGaussianNoise(
						new Float32Array(reference.length + Math.round(0.55 * SAMPLE_RATE)),
						0.8,
						42
					)
			}),
		/too weak to trust|outside 0/
	);
});
