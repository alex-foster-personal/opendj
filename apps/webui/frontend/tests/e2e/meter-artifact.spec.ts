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
 *   [if] a silent tap keeps posting                  [then STOP] idle costs main-thread work
 *   [if] the PRODUCTION createMasterMeterSource/attachMeterTaps/masterMeterReading
 *        chain does not move with a real _masterGain [then STOP] the master path
 *        (pin 5a5c3b8033d8, Sol threads 3967597181 / 3967976238) is exercised by
 *        a stub only, never the shipped code on a real Web Audio connection
 *   [if] the production master reading does not floor on silence, or does not
 *        fall back to silent once the tap is released [then STOP] the master
 *        reading path invents a level with nothing connected
 *   [if] a disposed context's arm failure marks the remounted context's meters
 *        unavailable [then STOP] a healthy meter renders as broken (thread 3967934084)
 *
 * NOT wired into any CI workflow yet (tracked by issue #1356, same as every
 * other unwired e2e config in this directory) - this is real, local evidence,
 * but weaker than a gated one until that issue lands it in CI.
 */
import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';
import { build } from 'esbuild';

import { METER_ARTIFACT_BUILD_DIR } from './playwright.meter-artifact.config';

const FRONTEND_ROOT = fileURLToPath(new URL('../../', import.meta.url));

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

			// Wait on the reported VALUE, not on a message count. Two reasons:
			// Chromium can take longer than any sleep worth writing to bring the
			// audio device up, and a silent window deliberately posts nothing, so
			// counting messages cannot express "it went quiet".
			const waitForPeak = async (
				ok: (peak: number) => boolean,
				what: string,
				timeoutMs: number
			): Promise<void> => {
				const deadline = Date.now() + timeoutMs;
				while (!(latestPeak !== null && ok(latestPeak))) {
					if (Date.now() > deadline) {
						throw new Error(`meter never reported ${what}; last peak was ${latestPeak}`);
					}
					await new Promise((r) => setTimeout(r, 20));
				}
			};

			const readings: number[] = [];
			for (const value of gains) {
				gain.gain.setValueAtTime(value, ctx.currentTime);
				// The oscillator runs at amplitude 1.0, so the gain IS the peak.
				const tol = Math.max(0.02, value * 0.1);
				await waitForPeak(
					(peak) => Math.abs(peak - value) <= tol,
					`a peak near ${value}`,
					15_000
				);
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
	const { posts, readings } = await measureAtGains(page, [0.5]);
	// The count is deliberately not a rate check: the reader now waits on the
	// VALUE and returns as soon as it settles, and a silent window posts
	// nothing at all. What this pins is that the hashed asset resolved and the
	// processor registered, which is exactly what a missing emit would break.
	expect(posts, 'the meter worklet posted nothing, so it never registered').toBeGreaterThan(0);
	expect(readings[0], 'the worklet registered but reported no level').toBeGreaterThan(0);
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

test('a silent tap posts nothing, so idle decks cost no main-thread work', async ({ page }) => {
	await page.goto('/index.html');
	// This is a REGRESSION GUARD with a measured origin. The processor used to
	// post on every window regardless of content: four taps on a stopped set
	// measured 197 messages a second, forever, because a deck with no track
	// still has a live filter chain feeding the tap. The reader decays toward
	// the floor on its own, so those posts bought nothing at all.
	const posts = await page.evaluate(
		async ({ moduleUrl }) => {
			const ctx = new AudioContext();
			if (ctx.state === 'suspended') await ctx.resume();
			await ctx.audioWorklet.addModule(moduleUrl);
			const sink = new GainNode(ctx, { gain: 0 });
			sink.connect(ctx.destination);
			// Explicit zero-valued source: the tap sees real channels of zeros,
			// which is what a loaded-but-stopped deck actually delivers. A tap
			// with nothing connected would pass for the wrong reason.
			const silent = new ConstantSourceNode(ctx, { offset: 0 });
			silent.start();
			let n = 0;
			for (let i = 0; i < 4; i += 1) {
				const node = new AudioWorkletNode(ctx, 'mdt-channel-meter', {
					numberOfInputs: 1,
					numberOfOutputs: 1,
					outputChannelCount: [1],
					processorOptions: { reportIntervalS: 0.02 }
				});
				node.port.onmessage = () => { n += 1; };
				silent.connect(node);
				node.connect(sink);
			}
			await new Promise((r) => setTimeout(r, 1000));
			n = 0;
			await new Promise((r) => setTimeout(r, 3000));
			await ctx.close();
			return n;
		},
		{ moduleUrl: meterProcessorAssetPath() }
	);
	expect(posts, `four silent taps posted ${posts} messages in 3s; expected none`).toBe(0);
});

// ---------------------------------------------------------------------------
// The MASTER path, driven through the PRODUCTION module (Sol threads
// 3967597181 and 3967976238, both P1 BLOCKING).
//
// The first objection was that `createMasterMeterSource({})` in the unit suite
// substitutes a plain object where an AudioNode belongs, so it executes zero
// Web Audio connection behaviour. The reply to that was an UNAVAILABLE report,
// which was wrong: node:test has no Web Audio, but THIS suite already runs a
// real AudioContext against the real built worklet asset in real Chromium.
//
// The second objection was that the first attempt at fixing it still hand-built
// an equivalent-looking graph and never called the shipped functions, so it
// would have stayed green with the master tap deleted outright. That is the one
// these tests close: `master-meter-browser-entry.ts` is bundled from the REAL
// `meter-tap.ts` source and loaded into the page, so `createMasterMeterSource`,
// `attachMeterTaps`, `masterMeterReading`, `releaseMasterMeterTap` and
// `markMetersUnavailable` below are the functions the app ships, executing
// against a real AudioContext, the real worklet, and real AudioNodes.
//
// STILL UNAVAILABLE, stated rather than papered over: this does not drive
// `audio-engine.svelte.ts`'s `_ensureGraph()`, so "the engine hands
// `_masterGain` (and not some other node) to `createMasterMeterSource`" is
// still covered only by the unit suite's source assertion. `_ensureGraph` is a
// private step of a `.svelte.ts` rune module reachable only through the
// `/performance` route, and that route cannot boot in a test yet because
// `tests/e2e/fixtures/root-playwright-data/` has no `state.db` fixture
// (issue #1356). What is proven here is everything from the tap's source node
// onward.
// ---------------------------------------------------------------------------

/** esbuild plugin resolving Vite's `?url` worklet import to the built asset. */
const viteUrlSuffixPlugin = {
	name: 'vite-url-suffix',
	setup(esbuild: import('esbuild').PluginBuild) {
		esbuild.onResolve({ filter: /\?url$/ }, (args) => ({
			path: args.path,
			namespace: 'vite-url-suffix'
		}));
		esbuild.onLoad({ filter: /.*/, namespace: 'vite-url-suffix' }, () => ({
			contents: `export default ${JSON.stringify(meterProcessorAssetPath())};`,
			loader: 'js'
		}));
	}
};

/**
 * Bundle a meter-artifact browser entry, with its `?url` worklet import
 * resolved to the hashed asset THIS build emitted.
 *
 * esbuild rather than Vite for the same reason `tests/unit/load-typescript.mjs`
 * uses it: one entry, in memory, no long-lived dependency-optimizer handles.
 */
async function bundleMeterHarnessEntry(relativeEntry: string): Promise<string> {
	const result = await build({
		entryPoints: [`${FRONTEND_ROOT}${relativeEntry}`],
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: `${FRONTEND_ROOT}src/lib` },
		bundle: true,
		format: 'esm',
		logLevel: 'silent',
		platform: 'browser',
		target: 'chrome120',
		plugins: [viteUrlSuffixPlugin],
		write: false
	});
	if (result.outputFiles.length !== 1) {
		throw new Error(`expected one bundled harness output, got ${result.outputFiles.length}`);
	}
	return result.outputFiles[0].text;
}

async function bundleMasterMeterHarness(): Promise<string> {
	return bundleMeterHarnessEntry('tests/e2e/fixtures/master-meter-browser-entry.ts');
}

async function bundleChannelMeterHarness(): Promise<string> {
	return bundleMeterHarnessEntry('tests/e2e/fixtures/channel-meter-browser-entry.ts');
}

/** Load the built app page and install the production meter module on it. */
async function pageWithMasterMeterModule(page: import('@playwright/test').Page): Promise<void> {
	await page.goto('/index.html');
	await page.addScriptTag({ content: await bundleMasterMeterHarness(), type: 'module' });
	await page.waitForFunction(() => window.__masterMeterHarness !== undefined);
}

/**
 * Drive the production master path: a real GainNode as `_masterGain`, handed to
 * the shipped `createMasterMeterSource`, attached by the shipped
 * `attachMeterTaps`, and read back through the shipped `masterMeterReading`.
 */
async function productionMasterReadingsAtGains(
	page: import('@playwright/test').Page,
	gains: number[]
): Promise<{ readings: number[]; afterRelease: number }> {
	return page.evaluate(async (gains: number[]) => {
		const harness = window.__masterMeterHarness;
		if (harness === undefined) throw new Error('the master meter harness did not install');
		const ctx = new AudioContext();
		if (ctx.state === 'suspended') await ctx.resume();

		const osc = new OscillatorNode(ctx, { frequency: 1000 });
		// The node the engine passes as `_masterGain`. Everything downstream of
		// here is the shipped code, not this test's idea of it.
		const masterGain = new GainNode(ctx, { gain: gains[0] });
		osc.connect(masterGain);
		osc.start();

		const source = harness.createMasterMeterSource(masterGain);
		await harness.attachMeterTaps(ctx, [source]);

		const settleAt = async (target: number): Promise<number> => {
			const deadline = Date.now() + 20_000;
			// Ballistics only advance when the reading is taken, so poll the
			// real accessor rather than sleeping and reading once. The window is
			// 0.2 dB rather than something looser because the PPM decay is
			// asymptotic: stopping early lands short of the target and would
			// understate the gain change this test measures.
			for (;;) {
				const reading = harness.masterMeterReading(harness.meterClockMs());
				if (Math.abs(reading.db - target) <= 0.2) return reading.db;
				if (Date.now() > deadline) {
					throw new Error(
						`master meter never settled near ${target} dBFS; last reading was ${reading.db}`
					);
				}
				await new Promise((r) => setTimeout(r, 20));
			}
		};

		const readings: number[] = [];
		for (const value of gains) {
			masterGain.gain.setValueAtTime(value, ctx.currentTime);
			// The oscillator runs at amplitude 1.0, so the gain IS the peak -
			// clamped at the meter's own floor, because the reading path floors
			// rather than running off to -Infinity and silence must be compared
			// against the value the module actually reports.
			const target = Math.max(
				20 * Math.log10(Math.max(value, 1e-7)),
				harness.SILENT_METER_READING.db
			);
			readings.push(await settleAt(target));
		}

		// The dispose contract, executed: once the tap is released the reading
		// path must fall back to "no graph", not keep reporting a dead tap.
		harness.releaseMasterMeterTap();
		const afterRelease = harness.masterMeterReading(harness.meterClockMs()).db;

		osc.stop();
		harness.teardownMeterTaps();
		await ctx.close();
		return { readings, afterRelease };
	}, gains);
}

test('the production master meter reads a real level and moves with the master gain', async ({
	page
}) => {
	await pageWithMasterMeterModule(page);
	// 0.5 -> 0.125 is exactly -12 dB: the master volume control's whole job.
	// This goes red if createMasterMeterSource hands back a different node, if
	// attachMeterTaps stops connecting its source, or if masterMeterReading
	// stops reading the tap it created.
	const { readings } = await productionMasterReadingsAtGains(page, [0.5, 0.125]);
	expect(readings[0], `master meter did not read -6 dBFS (${readings})`).toBeCloseTo(-6.02, 0);
	// A tap that never moved reads a delta of 0; the ballistic settle window is
	// +/-0.2 dB at each end, so this range bites hard while staying honest about
	// what a decaying meter can be pinned to.
	const delta = readings[0] - readings[1];
	expect(delta, `master gain change did not move the production reading (${readings})`)
		.toBeGreaterThan(11.4);
	expect(delta, `master gain change overshot (${readings})`).toBeLessThan(12.6);
});

test('the production master meter floors on silence, and reads silent once released', async ({
	page
}) => {
	await pageWithMasterMeterModule(page);
	const { readings, afterRelease } = await productionMasterReadingsAtGains(page, [0.5, 0]);
	const floor = await page.evaluate(
		() => window.__masterMeterHarness!.SILENT_METER_READING.db
	);
	expect(readings[1], 'a silent master bus must decay to the floor').toBeLessThanOrEqual(floor + 0.5);
	expect(afterRelease, 'a released tap must read the shared silent value').toBe(floor);
});

// ---------------------------------------------------------------------------
// The FAILURE path, with a failure the browser really produces (Sol thread
// 3968599841, P1 BLOCKING).
//
// The unit suite could only reach this path by handing `attachMeterTaps` a
// duck-typed context whose `addModule` returned a rejected promise - a
// fabricated dependency failure, which is exactly what the fail-closed test
// contract refuses. node:test has no Web Audio at all, so that is a capability
// it does not have rather than a shortcut that was taken.
//
// Chromium does have it. Connecting a node to a node from a DIFFERENT
// AudioContext is a real InvalidAccessError raised by the real implementation
// inside `attachMeterTaps`'s own `source.connect(node)`, so the arm fails for
// a browser's reason rather than a test's. (A closed context was tried first
// and rejected as a technique: Chromium's `addModule()` resolves happily on
// one, which the guard assertion below would have caught.)
// ---------------------------------------------------------------------------

test('a real worklet arm failure marks the meters unavailable, and only for its own context', async ({
	page
}) => {
	await pageWithMasterMeterModule(page);
	const result = await page.evaluate(async () => {
		const harness = window.__masterMeterHarness!;
		const notifications: boolean[] = [];
		const stop = harness.onMetersUnavailableChange((next) => notifications.push(next));

		const remounted = new AudioContext();
		if (remounted.state === 'suspended') await remounted.resume();
		const retired = new AudioContext();
		if (retired.state === 'suspended') await retired.resume();

		// A real, browser-raised failure inside attachMeterTaps: the source node
		// belongs to `remounted`, so connecting it to a node built on `retired`
		// is an InvalidAccessError from Chromium itself.
		let failure = '';
		try {
			await harness.attachMeterTaps(retired, [
				harness.createMasterMeterSource(new GainNode(remounted, { gain: 1 }))
			]);
		} catch (error) {
			failure = String(error);
		}
		// The engine's catch, performed here: the verdict belongs to the context
		// that produced it.
		const retiredAccepted = harness.markMetersUnavailable(retired);
		const unavailableAfterRealFailure = harness.metersUnavailable();
		const readingWhileBroken = harness.masterMeterReading(harness.meterClockMs());
		const brokenReadsUnavailable = readingWhileBroken === harness.UNAVAILABLE_METER_READING;

		// Route unmount, then a remount that arms cleanly.
		harness.teardownMeterTaps();
		harness.releaseMasterMeterTap();
		await retired.close();
		await harness.attachMeterTaps(remounted, [
			harness.createMasterMeterSource(new GainNode(remounted, { gain: 1 }))
		]);

		// The retired context finally gives up, after the remount.
		const staleAccepted = harness.markMetersUnavailable(retired);
		const unavailableAfterStale = harness.metersUnavailable();
		const healthyReadsUnavailable =
			harness.masterMeterReading(harness.meterClockMs()) === harness.UNAVAILABLE_METER_READING;

		stop();
		harness.releaseMasterMeterTap();
		harness.teardownMeterTaps();
		await remounted.close();
		return {
			failure,
			retiredAccepted,
			unavailableAfterRealFailure,
			brokenReadsUnavailable,
			staleAccepted,
			unavailableAfterStale,
			healthyReadsUnavailable,
			notifications
		};
	});

	expect(
		result.failure,
		'the arm must fail for a real browser reason; if this is empty the test is proving nothing'
	).toMatch(/InvalidAccessError|different audio context|Error/i);
	expect(result.retiredAccepted, 'the arming context owns its own failure').toBe(true);
	expect(result.unavailableAfterRealFailure).toBe(true);
	expect(
		result.brokenReadsUnavailable,
		'a genuinely broken meter must read UNAVAILABLE, not an ordinary silent reading'
	).toBe(true);
	expect(result.staleAccepted, 'a retired context must not own the live verdict').toBe(false);
	expect(result.unavailableAfterStale, 'the remounted meter must stay usable').toBe(false);
	expect(result.healthyReadsUnavailable, 'a healthy remount must not read UNAVAILABLE').toBe(false);
	// true when the real failure landed, false again when the remount armed:
	// this is what lets an idle component learn about a failure with no poll.
	expect(result.notifications).toEqual([true, false]);
});

// ---------------------------------------------------------------------------
// Issue #3529: the per-DECK channel meter must follow the channel fader.
//
// Source-level grep that `source: fader` exists would stay green if the tap
// were moved back to `high` in a refactor that kept the string elsewhere. This
// block drives the shipped `buildDeckChannelGraph` + `readMeterTap` through a
// real AudioContext so a misplaced tap or a disconnected fader goes red.
// ---------------------------------------------------------------------------

async function pageWithChannelMeterModule(page: import('@playwright/test').Page): Promise<void> {
	await page.goto('/index.html');
	await page.addScriptTag({ content: await bundleChannelMeterHarness(), type: 'module' });
	await page.waitForFunction(() => window.__channelMeterHarness !== undefined);
}

test('the production channel meter reads a real level at unity fader', async ({ page }) => {
	await pageWithChannelMeterModule(page);
	const floor = await page.evaluate(() => window.__channelMeterHarness!.SILENT_METER_READING.db);
	const { readings } = await page.evaluate(async () => {
		const harness = window.__channelMeterHarness!;
		return harness.readChannelMeterAtFaderGains([1]);
	});
	expect(readings[0], `unity fader should read above the floor (${readings})`).toBeGreaterThan(
		floor + 1
	);
});

test('the production channel meter floors on the next observation when the channel fader closes', async ({
	page
}) => {
	await pageWithChannelMeterModule(page);
	const floor = await page.evaluate(() => window.__channelMeterHarness!.SILENT_METER_READING.db);
	const result = await page.evaluate(async () => {
		const harness = window.__channelMeterHarness!;
		return harness.readChannelMeterFloorsOnNextObservation();
	});
	expect(result.unityDb, 'unity fader must establish a non-floor reading first').toBeGreaterThan(
		floor + 1
	);
	expect(
		result.closedObservationPeak,
		`a closed channel fader must post silence on the first silent worklet observation (peak=${result.closedObservationPeak})`
	).toBeLessThanOrEqual(1e-6);
	expect(
		result.closedObservationDb,
		`the next observation after a silent metering window must floor (${result.closedObservationDb} dBFS)`
	).toBeLessThanOrEqual(floor + 0.5);
});

test('halving the production channel fader drops the reading by about 6 dB', async ({ page }) => {
	await pageWithChannelMeterModule(page);
	const { readings } = await page.evaluate(async () => {
		const harness = window.__channelMeterHarness!;
		return harness.readChannelMeterAtFaderGains([1, 0.5]);
	});
	const delta = readings[0] - readings[1];
	expect(delta, `half fader should be ~6 dB below unity (${readings})`).toBeGreaterThan(5);
	expect(delta, `half fader overshot the -6 dB target (${readings})`).toBeLessThan(7);
});

// ---------------------------------------------------------------------------
// Codex thread 3969675622 (P2 BLOCKING): a processor that crashes AFTER arming.
//
// The arming promise has already RESOLVED by then, so `attachMeterTaps`'s
// catch can never see it. The node goes permanently silent and the reading
// decayed to an ordinary `silent` state - a dead meter rendering exactly like
// a quiet master bus, which is the masking AGENTS.md L244-L246 forbids and the
// same lie findings B and E each fixed by a different door.
//
// WHAT THIS PROVES AND WHAT IT DOES NOT. A `processorerror` cannot be provoked
// from JavaScript; only the browser raises one, when the processor's own
// constructor or `process()` throws. So the crash itself is Chromium's
// contract, not ours, and this test does not re-prove it. What it does prove
// is every part that IS ours: that the shipped `attachMeterTaps` installs a
// handler on the real node, that the handler routes through the
// context-scoped verdict, that a crash therefore reads UNAVAILABLE rather than
// silent, and that teardown removes the handler instead of leaking it.
// ---------------------------------------------------------------------------

test('a processor crash after arming reads unavailable, not silent', async ({ page }) => {
	await pageWithMasterMeterModule(page);
	const result = await page.evaluate(async () => {
		const harness = window.__masterMeterHarness!;
		const notifications: boolean[] = [];
		const stop = harness.onMetersUnavailableChange((next) => notifications.push(next));

		const ctx = new AudioContext();
		if (ctx.state === 'suspended') await ctx.resume();
		await harness.attachMeterTaps(ctx, [
			harness.createMasterMeterSource(new GainNode(ctx, { gain: 1 }))
		]);

		const node = harness.masterMeterNode();
		const handlerInstalled = node !== null && typeof node.onprocessorerror === 'function';
		// Control: the meter must be HEALTHY here, or the assertion after the
		// crash would pass for a reason that has nothing to do with the crash.
		const unavailableBeforeCrash = harness.metersUnavailable();

		// A synthetic dispatchEvent does NOT reach the onprocessorerror IDL
		// attribute in Chromium (measured here: the verdict stayed false), so
		// the crash is delivered the way the browser delivers it, by invoking
		// the handler the production code installed.
		node?.dispatchEvent(new Event('processorerror'));
		const unavailableAfterSyntheticDispatch = harness.metersUnavailable();
		node?.onprocessorerror?.call(node, new ErrorEvent('processorerror'));
		const unavailableAfterCrash = harness.metersUnavailable();
		const crashReadsUnavailable =
			harness.masterMeterReading(harness.meterClockMs()) === harness.UNAVAILABLE_METER_READING;

		// Teardown must not leave a handler pointing at a dead graph.
		const retired = node;
		harness.teardownMeterTaps();
		harness.releaseMasterMeterTap();
		const handlerClearedOnTeardown = retired !== null && retired.onprocessorerror === null;

		const remounted = new AudioContext();
		if (remounted.state === 'suspended') await remounted.resume();
		await harness.attachMeterTaps(remounted, [
			harness.createMasterMeterSource(new GainNode(remounted, { gain: 1 }))
		]);
		const healthyAfterRemount = !harness.metersUnavailable();

		stop();
		harness.releaseMasterMeterTap();
		harness.teardownMeterTaps();
		await ctx.close();
		await remounted.close();
		return {
			handlerInstalled,
			unavailableBeforeCrash,
			unavailableAfterSyntheticDispatch,
			unavailableAfterCrash,
			crashReadsUnavailable,
			handlerClearedOnTeardown,
			healthyAfterRemount,
			notifications
		};
	});

	expect(
		result.handlerInstalled,
		'the shipped attachMeterTaps must install onprocessorerror on the real node'
	).toBe(true);
	expect(
		result.unavailableBeforeCrash,
		'the meter must be healthy before the crash, or this test proves nothing'
	).toBe(false);
	expect(
		result.unavailableAfterSyntheticDispatch,
		'recorded, not required: Chromium does not route a synthetic dispatch to the IDL attribute'
	).toBe(false);
	expect(result.unavailableAfterCrash, 'a crashed processor must mark the meters unavailable').toBe(
		true
	);
	expect(
		result.crashReadsUnavailable,
		'a crashed meter must read UNAVAILABLE, not an ordinary silent reading'
	).toBe(true);
	expect(
		result.handlerClearedOnTeardown,
		'teardown must clear onprocessorerror rather than leak it with the dead graph'
	).toBe(true);
	expect(result.healthyAfterRemount, 'a clean remount must arm healthy').toBe(true);
	expect(result.notifications).toEqual([true, false]);
});
