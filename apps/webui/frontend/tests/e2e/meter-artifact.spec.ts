/**
 * The channel level meter worklet, proven against the built asset in a real
 * browser audio graph.
 *
 * WHAT THIS EXISTS TO CATCH. The meter it replaces was tapped BEFORE the trim
 * gain and the EQ, so no mixer control could move it: it reported how loud the
 * file was and nothing else. Unit tests cannot see that, because the bug was in
 * the graph, not the arithmetic. So this drives a real AudioContext and asserts
 * that a gain change UPSTREAM of the tap moves the reading, which is exactly
 * what trim and EQ do in the engine.
 *
 * Acceptance:
 *   [if] the worklet asset does not resolve or register [then STOP] no message arrives
 *   [if] a -6 dBFS source does not read -6 dBFS      [then STOP] the scale is wrong
 *   [if] upstream gain does not move the reading     [then STOP] the tap is misplaced
 *   [if] silence does not read as silence            [then STOP] the meter is inventing level
 */
import { readdirSync } from 'node:fs';

import { expect, test } from '@playwright/test';

import { METER_ARTIFACT_BUILD_DIR } from './playwright.meter-artifact.config';

/** Resolve the hashed worklet asset the build just emitted. */
function meterProcessorAssetPath(): string {
	const assetsDir = `${METER_ARTIFACT_BUILD_DIR}/_app/immutable/assets`;
	const match = readdirSync(assetsDir).find(
		(name) => name.startsWith('meter-processor.') && name.endsWith('.js')
	);
	if (match === undefined) {
		throw new Error(
			`no meter-processor asset in ${assetsDir}. The ?url import did not emit, ` +
				`so the worklet cannot load in the installed app either.`
		);
	}
	return `/_app/immutable/assets/${match}`;
}

/**
 * Drive the real worklet: oscillator -> gain -> meter tap, and report the peak
 * the tap posts at each requested upstream gain.
 */
async function measureAtGains(page: import('@playwright/test').Page, gains: number[]) {
	return page.evaluate(
		async ({ moduleUrl, gains }) => {
			const ctx = new AudioContext();
			if (ctx.state === 'suspended') await ctx.resume();
			await ctx.audioWorklet.addModule(moduleUrl);
			const meter = new AudioWorkletNode(ctx, 'mdt-channel-meter', {
				numberOfInputs: 1,
				numberOfOutputs: 1,
				outputChannelCount: [1],
				processorOptions: { reportIntervalS: 0.02 }
			});
			let latestPeak: number | null = null;
			let posts = 0;
			meter.port.onmessage = (event) => {
				latestPeak = (event.data as { peak: number }).peak;
				posts += 1;
			};
			const osc = new OscillatorNode(ctx, { frequency: 1000 });
			const gain = new GainNode(ctx, { gain: gains[0] });
			const sink = new GainNode(ctx, { gain: 0 });
			osc.connect(gain);
			gain.connect(meter);
			meter.connect(sink);
			sink.connect(ctx.destination);
			osc.start();

			// Wait for REAL posts rather than sleeping a fixed time: Chromium can
			// take longer than any sleep worth writing to bring the audio device
			// up, and a fixed sleep turns that startup latency into a flake that
			// looks like a dead worklet.
			const waitForPosts = async (count: number, timeoutMs: number): Promise<void> => {
				const target = posts + count;
				const deadline = Date.now() + timeoutMs;
				while (posts < target) {
					if (Date.now() > deadline) {
						throw new Error(
							`meter posted ${posts} messages, needed ${target} within ${timeoutMs}ms`
						);
					}
					await new Promise((r) => setTimeout(r, 20));
				}
			};

			await waitForPosts(1, 10_000);
			const readings: number[] = [];
			for (const value of gains) {
				gain.gain.setValueAtTime(value, ctx.currentTime);
				// Several windows, so the recorded value is a window fully inside
				// the new gain rather than one straddling the change.
				await waitForPosts(5, 10_000);
				readings.push(latestPeak ?? -1);
			}
			osc.stop();
			await ctx.close();
			return { readings, posts };
		},
		{ moduleUrl: meterProcessorAssetPath(), gains }
	);
}

const dbfs = (amplitude: number): number => 20 * Math.log10(Math.max(amplitude, 1e-7));

test('the worklet asset loads and posts observations', async ({ page }) => {
	await page.goto('/index.html');
	const { posts } = await measureAtGains(page, [0.5]);
	expect(posts, 'the meter worklet posted nothing, so it never registered').toBeGreaterThan(5);
});

test('a -6 dBFS source reads -6 dBFS', async ({ page }) => {
	await page.goto('/index.html');
	const { readings } = await measureAtGains(page, [0.5]);
	expect(dbfs(readings[0])).toBeCloseTo(-6.02, 0);
});

test('gain UPSTREAM of the tap moves the reading', async ({ page }) => {
	await page.goto('/index.html');
	// 0.5 -> 0.125 is exactly -12 dB. This is the trim knob and the EQ: the
	// old pre-trim tap would have returned the same number for both.
	const { readings } = await measureAtGains(page, [0.5, 0.125]);
	const delta = dbfs(readings[0]) - dbfs(readings[1]);
	expect(delta, `upstream gain change did not move the meter (${readings})`).toBeCloseTo(12, 0);
});

test('silence reads as silence, not as an invented floor', async ({ page }) => {
	await page.goto('/index.html');
	const { readings } = await measureAtGains(page, [0.5, 0]);
	expect(readings[1]).toBeLessThan(1e-4);
});
