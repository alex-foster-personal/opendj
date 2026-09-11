/**
 * PERF-PAINT-01 real-browser check. Run against a performance page with four
 * loaded, playing decks:
 *
 *   WAVE_RAF_PROBE_URL=http://127.0.0.1:5273/performance \
 *     node tests/live/wave-row-raf-attribution.mjs
 *
 * It wraps requestAnimationFrame before navigation, attributes callback wall
 * time by the WaveRow callback's stable name, samples exactly three seconds,
 * and fails rather than emitting a number from an unprepared page. This is a
 * live check, not a fixture or a mock: the supplied URL must be a real running
 * web UI whose four waveform rows are playing.
 */
import { chromium } from '@playwright/test';

const SAMPLE_MS = 3_000;
const MAX_MAIN_THREAD_SHARE = 0.15;
const targetUrl = process.env.WAVE_RAF_PROBE_URL;

if (targetUrl === undefined || targetUrl === '') {
	throw new Error('WAVE_RAF_PROBE_URL is required and must point at a real /performance page');
}

const browser = await chromium.launch({ headless: false });
try {
	const page = await browser.newPage();
	await page.addInitScript(() => {
		const original = window.requestAnimationFrame.bind(window);
		const samples = [];
		window.requestAnimationFrame = (callback) =>
			original((timestamp) => {
				const start = performance.now();
				callback(timestamp);
				if (callback.name === 'waveRowFrame') samples.push(performance.now() - start);
			});
		window.__mdtWaveRowRafSamples = samples;
	});
	await page.goto(targetUrl, { waitUntil: 'networkidle' });
	await page.waitForTimeout(SAMPLE_MS);
	const result = await page.evaluate((sampleMs) => {
		const samples = window.__mdtWaveRowRafSamples;
		if (samples === undefined || samples.length < 500) {
			throw new Error(`expected four active WaveRow callbacks over ${sampleMs}ms, got ${samples?.length ?? 0}`);
		}
		const callbackMs = samples.reduce((total, value) => total + value, 0);
		return { callbacks: samples.length, callbackMs, share: callbackMs / sampleMs };
	}, SAMPLE_MS);
	console.log(JSON.stringify(result));
	if (result.share >= MAX_MAIN_THREAD_SHARE) {
		throw new Error(`WaveRow rAF share ${(result.share * 100).toFixed(2)}% exceeds 15% budget`);
	}
} finally {
	await browser.close();
}
