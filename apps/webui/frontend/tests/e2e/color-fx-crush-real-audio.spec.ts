/**
 * CRUSH color FX (SNDCOL-10655), rendered by a real Web Audio graph.
 *
 * Inlined #9774 render checks for the crush kernel only, until that harness
 * lands. The unit tests (tests/unit/color-fx-crush.test.mjs) run the processor
 * in node; this spec loads the SHIPPED processor file into Chromium's
 * OfflineAudioContext from a Blob URL, builds a minimal channel the way
 * deck-channel-graph.ts wires it (high -> filterDry/filterLp/filterHp ->
 * colorBus) and drives it with the shipped driveColorChannel / spliceCrush, so
 * the worklet, the native highpass and the linear crossfade are all Chromium's.
 * Knob moves land at whole render quanta through ctx.suspend.
 *
 * Runs under the frontend playwright.config.ts; it visits one routed https page
 * (AudioWorklet needs a secure context) and makes no network request.
 *
 * Requirements:
 *
 * - ✔︎ No sample over 0 dBFS at COLOR 0, 0.15, 0.3, 0.7, 0.85, 1 x PARAMETER 0, 0.5, 1.
 * - ✔︎ No NaN, infinity or subnormal, including a 0 -> 1 -> 0 sweep in 100 ms at PARAMETER 0 and 1.
 * - ✔︎ The detent on CRUSH is bit-exact to FILTER bypass.
 * - ✔︎ Click ratio at or under 1.5 on engage, switch and release.
 * - ✔︎ Below -90 dB against dry from 50 ms after the knob returns to center.
 * - ✔︎ Added latency at most 128 samples.
 *
 * Acceptance tests:
 *
 * - [if] any rendered sample exceeds 1.0 at a grid position [then ⛔️] CRUSH clips.
 * - [if] any rendered sample is NaN, infinite or subnormal [then ⛔️] the kernel is numerically unsafe.
 * - [if] CRUSH at COLOR 0.5 differs from FILTER at 0.5 by any amount [then ⛔️] the detent is not bypass.
 * - [if] the 20 ms after engage, switch or release steps more than 1.5x the surrounding signal [then ⛔️] it clicks.
 * - [if] the output differs from dry by 3.16e-5 or more 50 ms after release [then ⛔️] a crushed tail remains.
 * - [if] the cross-correlation peak against dry lags over 128 samples [then ⛔️] CRUSH adds latency.
 */
import { expect, test, type Page } from '@playwright/test';
import { build } from 'esbuild';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PROCESSOR_PATH = `${FRONTEND_ROOT}/src/lib/rb/color-fx-crush-processor.js`;
const HARNESS_URL = 'https://crush-harness.test/';
const SR = 48_000;
const QUANTUM = 128;
const RENDER_SEC = 0.8;
const PEAK_MAX = 1.0;
const SUBNORMAL_MAX = 1.1754943508222875e-38;
const CLICK_RATIO_MAX = 1.5;
const RING_DOWN_MAX = 10 ** (-90 / 20);
const RING_DOWN_AFTER_SEC = 0.05;
const LATENCY_MAX_SAMPLES = 128;
const COLOR_GRID = [0, 0.15, 0.3, 0.7, 0.85, 1];
const PARAMETER_GRID = [0, 0.5, 1];
const CENTER = 0.5;

/** noise-sine and kick are the #9774 fixture set; noise (aperiodic) is for the latency correlation only. */
type Fixture = 'noise-sine' | 'kick' | 'noise';
/** [seconds (snapped to a quantum), effect, knob value, PARAMETER]. */
type KnobEvent = [number, 'filter' | 'crush', number, number];

/** The two calls this spec makes on the esbuild IIFE bundle; ``rt`` is a hand-built runtime, so it stays opaque. */
type CrushFxBundle = {
	spliceCrush(rt: unknown): void;
	driveColorChannel(
		rt: unknown,
		channel: unknown,
		prior: null,
		value: number,
		parameter: number
	): void;
};

async function crushBundle(): Promise<string> {
	const result = await build({
		stdin: {
			contents: [
				"export { driveColorChannel, spliceCrush } from './src/lib/rb/color-fx-graph';",
				"export { CRUSH_PARAMETER_NOMINAL } from './src/lib/rb/color-fx-crush';"
			].join('\n'),
			resolveDir: FRONTEND_ROOT,
			loader: 'ts'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: `${FRONTEND_ROOT}/src/lib` },
		bundle: true,
		format: 'iife',
		globalName: 'crushFx',
		platform: 'browser',
		write: false,
		logLevel: 'silent'
	});
	return result.outputFiles[0].text;
}

const snap = (seconds: number): number => (Math.round((seconds * SR) / QUANTUM) * QUANTUM) / SR;

/** Render the channel with `events` (the first is applied at t=0); `splice` loads and splices CRUSH. */
async function render(
	page: Page,
	fixture: Fixture,
	splice: boolean,
	events: KnobEvent[]
): Promise<number[][]> {
	return page.evaluate(
		async ({ fixture, splice, events, sr, seconds, processorSource }) => {
			const mod = (globalThis as unknown as { crushFx: CrushFxBundle }).crushFx;
			const frames = Math.round(sr * seconds);
			const ctx = new OfflineAudioContext(2, frames, sr);
			const buffer = ctx.createBuffer(2, frames, sr);
			let seed = 0x9e3779b9;
			const rand = (): number => {
				seed ^= seed << 13;
				seed ^= seed >>> 17;
				seed ^= seed << 5;
				return (seed >>> 0) / 4294967296 - 0.5;
			};
			for (let c = 0; c < 2; c++) {
				const data = buffer.getChannelData(c);
				for (let i = 0; i < frames; i++) {
					const t = i / sr;
					if (fixture === 'noise-sine')
						data[i] = 0.6 * rand() + 0.4 * Math.sin(2 * Math.PI * 440 * t + c);
					else if (fixture === 'kick')
						data[i] = Math.exp(-((t % 0.25) * 30)) * Math.sin(2 * Math.PI * 60 * (t % 0.25));
					else data[i] = rand();
				}
				let peak = 0;
				for (const v of data) peak = Math.max(peak, Math.abs(v));
				const scale = 10 ** (-3 / 20) / peak;
				for (let i = 0; i < frames; i++) data[i] *= scale;
			}
			const player = ctx.createBufferSource();
			player.buffer = buffer;
			player.start(0);

			const high = ctx.createGain();
			const filterLp = ctx.createBiquadFilter();
			filterLp.type = 'lowpass';
			const filterHp = ctx.createBiquadFilter();
			filterHp.type = 'highpass';
			const filterDry = ctx.createGain();
			const filterLpWet = ctx.createGain();
			filterLpWet.gain.value = 0;
			const filterHpWet = ctx.createGain();
			filterHpWet.gain.value = 0;
			const colorBus = ctx.createGain();
			player.connect(high);
			for (const stage of [filterDry, filterLp, filterHp]) high.connect(stage);
			filterLp.connect(filterLpWet);
			filterHp.connect(filterHpWet);
			for (const branch of [filterDry, filterLpWet, filterHpWet]) branch.connect(colorBus);
			colorBus.connect(ctx.destination);
			const rt = {
				nodes: {
					high,
					filterLp,
					filterHp,
					filterDry,
					filterLpWet,
					filterHpWet,
					colorBus,
					cue: ctx.createGain(),
					fader: ctx.createGain()
				},
				graph: null,
				crush: null,
				spaceQuietSince: null
			};
			if (splice) {
				const url = URL.createObjectURL(new Blob([processorSource], { type: 'text/javascript' }));
				await ctx.audioWorklet.addModule(url);
				mod.spliceCrush(rt);
			}
			const drive = ([, effect, value, parameter]: [number, string, number, number]): void =>
				mod.driveColorChannel(rt, { effect, echo_beats: 0.75 }, null, value, parameter);
			drive(events[0]);
			for (const event of events.slice(1)) {
				ctx.suspend(event[0]).then(() => {
					drive(event);
					ctx.resume();
				});
			}
			const rendered = await ctx.startRendering();
			return [0, 1].map((c) => Array.from(rendered.getChannelData(c)));
		},
		{
			fixture,
			splice,
			events,
			sr: SR,
			seconds: RENDER_SEC,
			processorSource: await readFile(PROCESSOR_PATH, 'utf8')
		}
	);
}

//----- metrics

const frameAt = (seconds: number): number => Math.round(seconds * SR);

function badSamples(channels: number[][]): { peak: number; nonFinite: number; subnormal: number } {
	let peak = 0;
	let nonFinite = 0;
	let subnormal = 0;
	for (const x of channels)
		for (const v of x) {
			if (!Number.isFinite(v)) nonFinite++;
			else if (v !== 0 && Math.abs(v) < SUBNORMAL_MAX) subnormal++;
			else peak = Math.max(peak, Math.abs(v));
		}
	return { peak, nonFinite, subnormal };
}

function maxDiffStep(x: number[], from: number, to: number): number {
	let worst = 0;
	for (let i = Math.max(1, from); i < Math.min(to, x.length); i++)
		worst = Math.max(worst, Math.abs(x[i] - x[i - 1]));
	return worst;
}

/** Inlined #9774 click ratio: worst step in the 20 ms after the event over the larger of 50 ms before and 50-100 ms after. */
function clickRatio(x: number[], eventSec: number): number {
	const at = frameAt(eventSec);
	const spike = maxDiffStep(x, at, at + frameAt(0.02));
	const before = maxDiffStep(x, at - frameAt(0.05), at);
	const after = maxDiffStep(x, at + frameAt(0.05), at + frameAt(0.1));
	return spike / Math.max(before, after);
}

function maxAbsDiff(a: number[], b: number[], from: number): number {
	let worst = 0;
	for (let i = from; i < a.length; i++) worst = Math.max(worst, Math.abs(a[i] - b[i]));
	return worst;
}

/** Lag (samples) of the cross-correlation peak of `out` against `dry` over 0..512. */
function correlationLag(dry: number[], out: number[]): number {
	const from = frameAt(0.2);
	const to = frameAt(0.7);
	let bestLag = 0;
	let best = -Infinity;
	for (let lag = 0; lag <= 512; lag++) {
		let sum = 0;
		for (let i = from; i < to; i++) sum += dry[i] * out[i + lag];
		if (sum > best) {
			best = sum;
			bestLag = lag;
		}
	}
	return bestLag;
}

//----- tests

test.describe('CRUSH color FX through a real OfflineAudioContext (SNDCOL-10655, inlined #9774 checks)', () => {
	test.beforeEach(async ({ page }) => {
		// AudioWorklet exists only in a secure context, which about:blank is not.
		await page.route(HARNESS_URL, (route) =>
			route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>crush</title>' })
		);
		await page.goto(HARNESS_URL);
		await page.addScriptTag({ content: await crushBundle() });
	});

	test('no sample over 0 dBFS, no NaN/infinity/subnormal across the COLOR x PARAMETER grid', async ({
		page
	}) => {
		for (const fixture of ['noise-sine', 'kick'] as Fixture[])
			for (const color of COLOR_GRID)
				for (const parameter of PARAMETER_GRID) {
					const out = await render(page, fixture, true, [[0, 'crush', color, parameter]]);
					const bad = badSamples(out);
					console.log(
						`grid ${fixture} COLOR ${color} PARAMETER ${parameter}: ${JSON.stringify(bad)}`
					);
					expect(bad.nonFinite).toBe(0);
					expect(bad.subnormal).toBe(0);
					expect(bad.peak).toBeLessThanOrEqual(PEAK_MAX);
				}
	});

	test('a 0 -> 1 -> 0 COLOR sweep in 100 ms stays finite and under 0 dBFS at PARAMETER 0 and 1', async ({
		page
	}) => {
		const startSec = snap(0.2);
		const steps = Math.round((0.1 * SR) / QUANTUM);
		for (const parameter of [0, 1]) {
			const events: KnobEvent[] = [[0, 'crush', CENTER, parameter]];
			for (let k = 0; k <= steps; k++) {
				const value = 1 - Math.abs(1 - (2 * k) / steps);
				events.push([startSec + (k * QUANTUM) / SR, 'crush', value, parameter]);
			}
			const out = await render(page, 'noise-sine', true, events);
			const bad = badSamples(out);
			console.log(`sweep PARAMETER ${parameter}: ${JSON.stringify(bad)}`);
			expect(bad.nonFinite).toBe(0);
			expect(bad.subnormal).toBe(0);
			expect(bad.peak).toBeLessThanOrEqual(PEAK_MAX);
		}
	});

	test('the detent on CRUSH is bit-exact to FILTER bypass', async ({ page }) => {
		const filter = await render(page, 'noise-sine', false, [[0, 'filter', CENTER, CENTER]]);
		const crush = await render(page, 'noise-sine', true, [[0, 'crush', CENTER, CENTER]]);
		for (const c of [0, 1]) expect(maxAbsDiff(crush[c], filter[c], 0)).toBe(0);
	});

	test('engage, switch and release stay under the click ratio; release rings down below -90 dB in 50 ms', async ({
		page
	}) => {
		const engage = snap(0.2);
		const release = snap(0.5);
		const nominal = 0.5;
		const engaged = await render(page, 'noise-sine', true, [
			[0, 'crush', CENTER, nominal],
			[engage, 'crush', 0.15, nominal],
			[release, 'crush', CENTER, nominal]
		]);
		const switched = await render(page, 'noise-sine', true, [
			[0, 'filter', 0.15, nominal],
			[engage, 'crush', 0.15, nominal]
		]);
		const dry = await render(page, 'noise-sine', false, [[0, 'filter', CENTER, nominal]]);
		const ratios = {
			engage: clickRatio(engaged[0], engage),
			switch: clickRatio(switched[0], engage),
			release: clickRatio(engaged[0], release)
		};
		console.log(`click ratios: ${JSON.stringify(ratios)}`);
		for (const ratio of Object.values(ratios)) expect(ratio).toBeLessThanOrEqual(CLICK_RATIO_MAX);
		const tailFrom = frameAt(release + RING_DOWN_AFTER_SEC);
		const tail = Math.max(
			maxAbsDiff(engaged[0], dry[0], tailFrom),
			maxAbsDiff(engaged[1], dry[1], tailFrom)
		);
		console.log(`ring-down vs dry from ${RING_DOWN_AFTER_SEC * 1000} ms after release: ${tail}`);
		expect(tail).toBeLessThan(RING_DOWN_MAX);
	});

	test('added latency is at most 128 samples at COLOR 0.15 and 0.85', async ({ page }) => {
		const dry = await render(page, 'noise', false, [[0, 'filter', CENTER, CENTER]]);
		for (const color of [0.15, 0.85]) {
			const out = await render(page, 'noise', true, [[0, 'crush', color, CENTER]]);
			const lag = correlationLag(dry[0], out[0]);
			console.log(`latency COLOR ${color}: ${lag} samples`);
			expect(lag).toBeLessThanOrEqual(LATENCY_MAX_SAMPLES);
		}
	});
});
