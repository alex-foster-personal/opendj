// requirement: IOPIN-08
// [if] no consumer holds the meters [then] nothing samples on its own, even while real audio plays
// [if] the I/O panel (a consumer) holds the meters [then] the real analysers are sampled about
//   every SIGNAL_METER_INTERVAL_MS, and a signal that appears is published within a tick or two
// [if] the last consumer releases [then] sampling stops (reference counted)
// [if] a non-reactive reader refreshes [then] one real sample is taken, rate-limited to the interval,
//   and no sampling continues after it
// [if] a reading changes by any amount [then] it is published exactly (no quantizing), so an IPC
//   reader can still tell 0 from 0.0003 [⛔️ if an exact-zero cue reading could read stale]
// [if] the audio clock stops (context suspended) [then] the meters read inactive, not stale
//
// Production path only (AGENTS.md "No mocks and locked real fixtures"): the
// meter tests run the real `ensureHeadphoneGraph` in headless Chromium, on a
// real AudioContext, fed by a real MP3 fixture decoded by the browser, with
// the browser's own timers and clock. Nothing is stood in or patched. When
// Playwright or Chromium is missing, or the context cannot run, the test is
// skipped with an `UNAVAILABLE:` reason; it never passes without measuring.
// Not a hard failure under CI: the runner pool is mixed, and some runners
// (bifrost1-wsl-3, Mon 2 Nov 2026) have no Playwright Chromium build, so a
// failure there would be an infrastructure red, not a regression.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { after, before, test } from 'node:test';

import { bundleTypeScriptModule, loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const AUDIO_FIXTURE = path.resolve(
	FRONTEND_ROOT,
	'../../../tests/fixtures/app-acceptance/library/01-acceptance-pulse.mp3'
);

let headphones;
let state;

before(async () => {
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
	state = await loadTypeScriptModule('src/lib/player/state.svelte.ts');
});

const signals = () => state.mixerState.headphones.signals;

test('signal publish is exact: a change too small for the lamp still reaches the snapshot', async () => {
	const signal = signals().input;
	headphones.clearCalibrationInputSignal();
	headphones.publishCalibrationInputSignal(new Float32Array(256).fill(0.0003));
	assert.equal(signal.state, 'measured');
	assert.ok(Math.abs(signal.rms - 0.0003) < 1e-9);
	const firstStamp = signal.measured_at;
	await new Promise((resolve) => setTimeout(resolve, 5));
	headphones.publishCalibrationInputSignal(new Float32Array(256));
	assert.equal(
		signal.rms,
		0,
		'if 0.0003 -> 0 is swallowed then an exact-zero IPC reading is stale'
	);
	assert.notEqual(signal.measured_at, firstStamp, 'measured_at stamps the latest measurement');
	headphones.clearCalibrationInputSignal();
	assert.equal(signal.state, 'inactive');
	assert.equal(signal.measured_at, null);
});

//-----------------------------------------------------------------------------
// real browser, real AudioContext, real decoded audio, real timers
//-----------------------------------------------------------------------------

let server = null;
let origin = null;
let browserOutcome = null;

const PAGE_HTML = '<!doctype html><meta charset="utf-8"><title>meter gate</title><body></body>';

before(async () => {
	const bundle = await bundleTypeScriptModule('tests/unit/headphone-meter-browser-entry.mjs');
	const audio = readFileSync(AUDIO_FIXTURE);
	server = createServer((request, response) => {
		if (request.url === '/') {
			response.writeHead(200, { 'content-type': 'text/html' });
			response.end(PAGE_HTML);
		} else if (request.url === '/meters.js') {
			response.writeHead(200, { 'content-type': 'text/javascript' });
			response.end(bundle);
		} else if (request.url === '/pulse.mp3') {
			response.writeHead(200, { 'content-type': 'audio/mpeg' });
			response.end(audio);
		} else {
			response.writeHead(404);
			response.end();
		}
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	origin = `http://127.0.0.1:${server.address().port}`;

	let playwright;
	try {
		const require = createRequire(path.join(FRONTEND_ROOT, 'package.json'));
		playwright = await import(pathToFileURL(require.resolve('@playwright/test')).href);
	} catch (error) {
		browserOutcome = { skip: `UNAVAILABLE: @playwright/test is not installed: ${error.message}` };
		return;
	}
	try {
		// The Chromium build Playwright pins is used by default; a machine whose
		// browser cache holds a different build names one explicitly.
		const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;
		const browser = await (playwright.default ?? playwright).chromium.launch({
			headless: true,
			executablePath,
			args: ['--autoplay-policy=no-user-gesture-required']
		});
		browserOutcome = { browser };
	} catch (error) {
		browserOutcome = {
			skip: `UNAVAILABLE: Chromium could not launch (set PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH to use another build): ${error.message.split('\n')[0]}`
		};
	}
});

after(async () => {
	await browserOutcome?.browser?.close();
	await new Promise((resolve) => (server === null ? resolve() : server.close(resolve)));
});

/**
 * Open a page that imports the production headphone module, builds its real
 * monitor graph on a running AudioContext, and has the decoded fixture ready
 * to play into the master bus. Returns null (and skips) when the browser or
 * the audio stack is unavailable.
 */
async function openMeterPage(t) {
	if (browserOutcome.skip !== undefined) {
		t.skip(browserOutcome.skip);
		return null;
	}
	const page = await browserOutcome.browser.newPage();
	t.after(() => page.close());
	const pageErrors = [];
	page.on('pageerror', (error) => pageErrors.push(error));
	t.after(() => assert.deepEqual(pageErrors.map(String), [], 'the page threw'));
	await page.goto(`${origin}/`);
	const ready = await page.evaluate(async () => {
		// The loader's rune shim (load-typescript.mjs): `$state` is identity.
		globalThis.__musicDjToolsTestState = (value) => value;
		globalThis.__musicDjToolsTestState.snapshot = (value) =>
			value === undefined ? undefined : JSON.parse(JSON.stringify(value));
		const meters = await import('/meters.js');
		if (typeof AudioContext !== 'function') return { unavailable: 'AudioContext is not defined' };
		const context = new AudioContext();
		await context.resume();
		if (context.state !== 'running')
			return { unavailable: `AudioContext state is ${context.state}` };
		const encoded = await (await fetch('/pulse.mp3')).arrayBuffer();
		let decoded;
		try {
			decoded = await context.decodeAudioData(encoded);
		} catch (error) {
			return { unavailable: `MP3 decode failed: ${error}` };
		}
		const masterGain = context.createGain();
		masterGain.connect(context.destination);
		meters.ensureHeadphoneGraph(context, masterGain);
		let source = null;
		globalThis.meterHarness = {
			meters,
			context,
			play() {
				source = context.createBufferSource();
				source.buffer = decoded;
				source.loop = true;
				source.connect(masterGain);
				source.start();
			},
			stop() {
				source?.stop();
				source?.disconnect();
				source = null;
			},
			master() {
				return { ...meters.mixerState.headphones.signals.master };
			},
			/** Distinct master `measured_at` stamps seen over `ms` of real time. */
			async stampsOver(ms) {
				const seen = new Set();
				const until = performance.now() + ms;
				while (performance.now() < until) {
					const stamp = meters.mixerState.headphones.signals.master.measured_at;
					if (stamp !== null) seen.add(stamp);
					await new Promise((resolve) => setTimeout(resolve, 10));
				}
				return seen.size;
			},
			/** Resolve once the master reading satisfies `predicate`, or time out. */
			async until(predicate, ms) {
				const deadline = performance.now() + ms;
				while (performance.now() < deadline) {
					if (predicate(meters.mixerState.headphones.signals.master)) return true;
					await new Promise((resolve) => setTimeout(resolve, 10));
				}
				return false;
			},
			sleep(ms) {
				return new Promise((resolve) => setTimeout(resolve, ms));
			}
		};
		return { ok: true };
	});
	if (ready.unavailable !== undefined) {
		t.skip(`UNAVAILABLE: ${ready.unavailable}`);
		return null;
	}
	return page;
}

test('with no consumer the meters do not sample, even while real audio plays', async (t) => {
	const page = await openMeterPage(t);
	if (page === null) return;
	const result = await page.evaluate(async () => {
		const h = globalThis.meterHarness;
		h.play();
		const idleStamps = await h.stampsOver(800);
		const idle = h.master();
		// Control, same page and audio: a consumer makes the very same probe see samples.
		const release = h.meters.acquireHeadphoneSignalMeters();
		const heard = await h.until((s) => s.state === 'measured' && s.rms > 0, 1500);
		const heldStamps = await h.stampsOver(800);
		release();
		return { idleStamps, idle, heard, heldStamps };
	});
	assert.equal(
		result.idleStamps,
		0,
		'if a sample lands with the I/O panel closed then the idle clock is back'
	);
	assert.notEqual(result.idle.state, 'measured');
	assert.equal(result.idle.measured_at, null);
	assert.equal(
		result.heard,
		true,
		'control: the real audio reaches the master analyser once a consumer holds it'
	);
	assert.ok(
		result.heldStamps >= 3,
		`control: a held meter samples repeatedly (saw ${result.heldStamps} stamps in 800 ms)`
	);
});

test('a consumer samples the real analysers on the meter interval; the last release stops it', async (t) => {
	const page = await openMeterPage(t);
	if (page === null) return;
	const result = await page.evaluate(async () => {
		const h = globalThis.meterHarness;
		const interval = h.meters.SIGNAL_METER_INTERVAL_MS;
		const first = h.meters.acquireHeadphoneSignalMeters();
		// Acquire samples at once: silence on a running clock is a measured zero.
		const atAcquire = h.master();
		// A signal that appears shows within a couple of ticks.
		h.play();
		const t0 = performance.now();
		const appeared = await h.until((s) => s.rms !== null && s.rms > 0, interval * 4);
		const appearMs = performance.now() - t0;
		const heldStamps = await h.stampsOver(1000);
		// Reference counted: a second holder keeps it alive past the first release.
		const second = h.meters.acquireHeadphoneSignalMeters();
		first();
		const oneHolderStamps = await h.stampsOver(600);
		second();
		await h.sleep(interval);
		const releasedStamps = await h.stampsOver(800);
		first();
		second();
		return { interval, atAcquire, appeared, appearMs, heldStamps, oneHolderStamps, releasedStamps };
	});
	assert.equal(
		result.atAcquire.state,
		'measured',
		'acquire samples at once, not one interval later'
	);
	assert.equal(result.atAcquire.rms, 0, 'nothing is playing yet: the real analyser reads silence');
	assert.equal(
		result.appeared,
		true,
		`a signal that appeared was not published within ${result.interval * 4} ms`
	);
	// About 1000 / interval samples a second; allow for headless timer jitter both ways.
	const expected = 1000 / result.interval;
	assert.ok(
		result.heldStamps >= expected / 2,
		`held for 1 s: ${result.heldStamps} samples, expected about ${expected}`
	);
	assert.ok(
		result.heldStamps <= expected * 2,
		`held for 1 s: ${result.heldStamps} samples, expected about ${expected} (not a frame loop)`
	);
	assert.ok(
		result.oneHolderStamps >= 3,
		'if the first release stops a meter the second still holds then the panel goes dark'
	);
	assert.ok(
		result.releasedStamps <= 1,
		`if sampling survives the last release then the idle clock is back (saw ${result.releasedStamps} stamps)`
	);
});

test('a suspended audio clock reads inactive, not a stale level', async (t) => {
	const page = await openMeterPage(t);
	if (page === null) return;
	const result = await page.evaluate(async () => {
		const h = globalThis.meterHarness;
		h.play();
		const release = h.meters.acquireHeadphoneSignalMeters();
		const live = await h.until((s) => s.state === 'measured' && s.rms > 0, 1500);
		await h.context.suspend();
		const wentInactive = await h.until((s) => s.state === 'inactive', 1500);
		const suspended = h.master();
		await h.context.resume();
		const recovered = await h.until((s) => s.state === 'measured' && s.rms > 0, 1500);
		release();
		return { live, wentInactive, suspended, recovered };
	});
	assert.equal(result.live, true);
	assert.equal(
		result.wentInactive,
		true,
		'if a stopped clock keeps its last level then the lamp lies'
	);
	assert.equal(result.suspended.rms, null);
	assert.equal(result.recovered, true, 'control: the meter reads again once the clock runs');
});

test('on-demand refresh with no consumer takes one real sample, rate-limited, and starts nothing', async (t) => {
	const page = await openMeterPage(t);
	if (page === null) return;
	const result = await page.evaluate(async () => {
		const h = globalThis.meterHarness;
		const interval = h.meters.SIGNAL_METER_INTERVAL_MS;
		h.play();
		await h.sleep(200);
		const before = h.master();
		h.meters.refreshHeadphoneSignals();
		const first = h.master();
		await h.sleep(30);
		h.meters.refreshHeadphoneSignals();
		const burst = h.master();
		await h.sleep(interval + 20);
		h.meters.refreshHeadphoneSignals();
		const later = h.master();
		const afterStamps = await h.stampsOver(600);
		return { before, first, burst, later, afterStamps };
	});
	assert.equal(result.before.measured_at, null, 'nothing sampled before the refresh');
	assert.equal(result.first.state, 'measured', 'a refresh takes a real sample');
	assert.equal(
		result.burst.measured_at,
		result.first.measured_at,
		'a second read inside one interval reuses the last sample'
	);
	assert.notEqual(
		result.later.measured_at,
		result.first.measured_at,
		'a read one interval later samples again'
	);
	assert.equal(
		result.afterStamps,
		1,
		'if a refresh starts the sampler then the idle clock is back'
	);
});
