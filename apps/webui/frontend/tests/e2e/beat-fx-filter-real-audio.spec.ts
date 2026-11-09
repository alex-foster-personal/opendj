/**
 * Beat FX FILTER kernel (FXFILT-08), rendered by a real Web Audio graph.
 *
 * The unit tests (tests/unit/beat-fx-filter-kernel.test.mjs) check the kernel's math and
 * the nodes it schedules on a fake context. This spec renders the PRODUCTION kernel
 * (src/lib/rb/fx/beat-fx-filter-kernel.ts) through Chromium's OfflineAudioContext at
 * 48 kHz and reads the samples back: the oscillator, the detune-driven lowpass and the
 * raised-cosine level ramps are Chromium's own. Level moves run inside ctx.suspend at
 * whole render quanta and read ctx.currentTime, as a host would. Fixtures peak at
 * -6 dBFS (a 440 Hz sine at 0.5 and seeded white noise scaled to 0.5).
 *
 * A control subject (the same engage/release as setValueAtTime steps) must fail the click
 * bound, so a passing kernel run means the detector can see a click. Measured values sit
 * beside each threshold (Mon 9 Nov 2026).
 *
 * Runs under playwright.config.ts; it only visits about:blank.
 *
 * Requirements:
 *
 * - ✔︎ ✅ 🎯 Level 0 output is bit-exact to the input.
 * - ✔︎ ✅ 🎯 No sample over 0 dBFS, no NaN, infinity or subnormal at level 1.
 * - ✔︎ ✅ 🎯 The LFO trough lands on the grid within 1 ms; the period is within 1 ms of the division.
 * - ✔︎ ✅ 🎯 Engage and release score a click ratio at or under 1.5.
 * - ✔︎ ✅ CPU cost of the kernel against a bare gain is logged (#9777 row).
 *
 * Acceptance tests:
 *
 * - [if] level 0 output differs from the input by any amount [then ⛔️] the dry path is not bit-exact.
 * - [if] a level-1 render has |x| > 1, NaN, infinity or a subnormal [then ⛔️] the kernel is unsafe.
 * - [if] a detune trough misses its grid beat by over 1 ms at 120 or 174 BPM [then ⛔️] the LFO is not beat-locked.
 * - [if] trough spacing misses beats * 60 / bpm (or TIME) by over 1 ms [then ⛔️] the period is wrong.
 * - [if] engage or release scores a click ratio over 1.5 [then ⛔️] the level ramp clicks.
 * - [if] the step control scores at or under 1.5 [then ⛔️] the click detector is blind.
 */
import { expect, test, type Page } from '@playwright/test';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SR = 48_000;
const QUANTUM = 128;
/** Click bound (issue #9792). Measured Mon 9 Nov 2026: kernel engage 0.81, release 0.87; step control 140, 150. */
const CLICK_RATIO_MAX = 1.5;
const MS = 1e-3;
const SUBNORMAL = 1.1754943508222875e-38;
const ENGAGE_SEC = (188 * QUANTUM) / SR;
const RELEASE_SEC = (563 * QUANTUM) / SR;

type Fixture = 'sine' | 'noise';
type Clock = { beats: number; bpm: number | null; timeMs: number };
type Plan = {
	fixture: Fixture;
	clock: Clock;
	seconds: number;
	channels: number;
	/** 'static' sets level once at 0; 'switch' engages and releases; 'step' is the control. */
	level: { kind: 'static'; value: number } | { kind: 'switch' | 'step' };
};

async function kernelBundle(): Promise<string> {
	const result = await build({
		stdin: {
			contents: "export * from './src/lib/rb/fx/beat-fx-filter-kernel';",
			resolveDir: FRONTEND_ROOT,
			loader: 'ts'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: `${FRONTEND_ROOT}/src/lib` },
		bundle: true,
		format: 'iife',
		globalName: 'beatFxFilter',
		platform: 'browser',
		write: false,
		logLevel: 'silent'
	});
	return result.outputFiles[0].text;
}

/** Returns channels [output, input, detune cents]. */
async function render(page: Page, plan: Plan): Promise<number[][]> {
	return page.evaluate(
		async ({ plan, sr, engageSec, releaseSec }) => {
			// eslint-disable-next-line @typescript-eslint/no-explicit-any
			const mod = (globalThis as any).beatFxFilter;
			const frames = Math.round(sr * plan.seconds);
			const ctx = new OfflineAudioContext(3, frames, sr);
			const buffer = ctx.createBuffer(1, frames, sr);
			const data = buffer.getChannelData(0);
			if (plan.fixture === 'sine') {
				for (let i = 0; i < frames; i++) data[i] = 0.5 * Math.sin((2 * Math.PI * 440 * i) / sr);
			} else {
				let seed = 12345;
				let peak = 0;
				for (let i = 0; i < frames; i++) {
					seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
					data[i] = seed / 2 ** 31 - 1;
					peak = Math.max(peak, Math.abs(data[i]));
				}
				for (let i = 0; i < frames; i++) data[i] *= 0.5 / peak;
			}
			const player = ctx.createBufferSource();
			player.buffer = buffer;
			player.start(0);

			const merger = ctx.createChannelMerger(3);
			merger.connect(ctx.destination);
			const kernel = mod.buildBeatFxFilterKernel(ctx);
			player.connect(kernel.input);
			player.connect(merger, 0, 1);
			kernel.output.connect(merger, 0, 0);
			kernel.lfoDepth.connect(merger, 0, 2);

			const { bpm } = plan.clock;
			const grid =
				bpm === null
					? null
					: {
							beats: Array.from({ length: Math.ceil((plan.seconds * bpm) / 60) + 8 }, (_, i) => ({
								n: (i % 4) + 1,
								bpm,
								t: (i * 60) / bpm
							})),
							positionSec: 0,
							bpm
						};
			kernel.setClock(
				mod.resolveBeatFxFilterClock({
					beats: plan.clock.beats,
					timeMs: plan.clock.timeMs,
					grid,
					contextTime: 0
				}),
				0
			);
			const level = plan.level;
			if (level.kind === 'static') {
				kernel.setLevel(level.value, 0);
			} else {
				const apply = (value: number) => {
					const now = ctx.currentTime;
					if (level.kind === 'switch') {
						kernel.setLevel(value, now);
					} else {
						kernel.wet.gain.setValueAtTime(value, now);
						kernel.dry.gain.setValueAtTime(1 - value, now);
					}
				};
				for (const [at, value] of [
					[engageSec, 1],
					[releaseSec, 0]
				]) {
					ctx.suspend(at).then(() => {
						apply(value);
						ctx.resume();
					});
				}
			}
			const rendered = await ctx.startRendering();
			return [0, 1, 2].map((c) => Array.from(rendered.getChannelData(c)));
		},
		{ plan, sr: SR, engageSec: ENGAGE_SEC, releaseSec: RELEASE_SEC }
	);
}

//----- metrics

const frameAt = (seconds: number): number => Math.round(seconds * SR);

function maxSecondDiff(x: number[], fromSec: number, toSec: number): number {
	let worst = 0;
	for (let i = Math.max(1, frameAt(fromSec)); i < Math.min(x.length - 1, frameAt(toSec)); i++)
		worst = Math.max(worst, Math.abs(x[i + 1] - 2 * x[i] + x[i - 1]));
	return worst;
}

/** Event window (1 ms lead: a step at frame n shows at n - 1) second-difference peak over the larger steady peak in the 100 ms either side. */
function clickRatio(x: number[], eventSec: number): number {
	const event = maxSecondDiff(x, eventSec - 0.001, eventSec + 0.015);
	const steady = Math.max(
		maxSecondDiff(x, eventSec - 0.101, eventSec - 0.001),
		maxSecondDiff(x, eventSec + 0.015, eventSec + 0.115)
	);
	return event / steady;
}

/**
 * Times (s) of the detune trace's troughs: the midpoint of each run within 1% of its
 * minimum. A slow wavetable oscillator is not strictly monotonic sample to sample, so a
 * local-minimum scan finds false troughs; the run midpoint is symmetric about the true one.
 */
function troughTimes(cents: number[], fromSec: number): number[] {
	let low = 0;
	for (const c of cents) low = Math.min(low, c);
	const near = 0.99 * low;
	const out: number[] = [];
	let runStart = -1;
	for (let i = frameAt(fromSec); i < cents.length; i++) {
		if (cents[i] <= near && runStart < 0) runStart = i;
		else if (cents[i] > near && runStart >= 0) {
			out.push((runStart + i - 1) / 2 / SR);
			runStart = -1;
		}
	}
	return out;
}

function peakAbs(x: number[]): number {
	let peak = 0;
	for (const v of x) peak = Math.max(peak, Math.abs(v));
	return peak;
}

//----- tests

test.describe('beat FX FILTER kernel (real audio)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('about:blank');
		await page.addScriptTag({ content: await kernelBundle() });
	});

	test('level 0 is bit-exact dry', async ({ page }) => {
		for (const fixture of ['noise', 'sine'] as const) {
			const [out, input] = await render(page, {
				fixture,
				clock: { beats: 1, bpm: 120, timeMs: 500 },
				seconds: 2,
				channels: 3,
				level: { kind: 'static', value: 0 }
			});
			let diff = 0;
			for (let i = 0; i < out.length; i++) diff = Math.max(diff, Math.abs(out[i] - input[i]));
			expect(diff).toBe(0);
		}
	});

	test('level 1 stays under 0 dBFS with no NaN, infinity or subnormal', async ({ page }) => {
		for (const clock of [
			{ beats: 1 / 16, bpm: 174, timeMs: 500 },
			{ beats: 1, bpm: 120, timeMs: 500 }
		]) {
			for (const fixture of ['noise', 'sine'] as const) {
				const [out] = await render(page, {
					fixture,
					clock,
					seconds: 4,
					channels: 3,
					level: { kind: 'static', value: 1 }
				});
				const bad = out.filter(
					(x) => !Number.isFinite(x) || Math.abs(x) > 1 || (x !== 0 && Math.abs(x) < SUBNORMAL)
				);
				console.log(`safety ${fixture} ${JSON.stringify(clock)}: peak ${peakAbs(out)}`);
				expect(bad.length).toBe(0);
			}
		}
	});

	test('LFO troughs land on the grid; period matches the division and TIME', async ({ page }) => {
		for (const bpm of [120, 174]) {
			for (const beats of [1 / 4, 1, 4]) {
				const [, , cents] = await render(page, {
					fixture: 'sine',
					clock: { beats, bpm, timeMs: 500 },
					seconds: 4.5,
					channels: 3,
					level: { kind: 'static', value: 1 }
				});
				const periodSec = (beats * 60) / bpm;
				const troughs = troughTimes(cents, periodSec / 2);
				expect(troughs.length).toBeGreaterThanOrEqual(1);
				for (const t of troughs) {
					const cycles = t / periodSec;
					expect(
						Math.abs(cycles - Math.round(cycles)) * periodSec,
						`bpm ${bpm} beats ${beats} at ${t}`
					).toBeLessThanOrEqual(MS);
				}
				for (let k = 1; k < troughs.length; k++)
					expect(Math.abs(troughs[k] - troughs[k - 1] - periodSec)).toBeLessThanOrEqual(MS);
			}
		}
		const [, , cents] = await render(page, {
			fixture: 'sine',
			clock: { beats: 1, bpm: null, timeMs: 500 },
			seconds: 2.2,
			channels: 3,
			level: { kind: 'static', value: 1 }
		});
		const troughs = troughTimes(cents, 0.25);
		expect(troughs.length).toBeGreaterThanOrEqual(3);
		for (let k = 1; k < troughs.length; k++)
			expect(Math.abs(troughs[k] - troughs[k - 1] - 0.5)).toBeLessThanOrEqual(MS);
	});

	test('engage and release score a click ratio at or under 1.5; the step control does not', async ({
		page
	}) => {
		const clock = { beats: 1, bpm: 120, timeMs: 500 };
		const [kernelOut] = await render(page, {
			fixture: 'sine',
			clock,
			seconds: 2,
			channels: 3,
			level: { kind: 'switch' }
		});
		const [stepOut] = await render(page, {
			fixture: 'sine',
			clock,
			seconds: 2,
			channels: 3,
			level: { kind: 'step' }
		});
		const kernel = [clickRatio(kernelOut, ENGAGE_SEC), clickRatio(kernelOut, RELEASE_SEC)];
		const step = [clickRatio(stepOut, ENGAGE_SEC), clickRatio(stepOut, RELEASE_SEC)];
		console.log(
			`click ratio: kernel ${kernel.join(', ')}; step control ${step.join(', ')}; bound ${CLICK_RATIO_MAX}`
		);
		for (const r of kernel) expect(r).toBeLessThanOrEqual(CLICK_RATIO_MAX);
		expect(Math.max(...step)).toBeGreaterThan(CLICK_RATIO_MAX);
	});

	test('CPU: kernel render cost against a bare gain (#9777 row)', async ({ page }) => {
		const timing = await page.evaluate(
			async ({ sr }) => {
				// eslint-disable-next-line @typescript-eslint/no-explicit-any
				const mod = (globalThis as any).beatFxFilter;
				const frames = sr * 10;
				async function once(withKernel: boolean): Promise<number> {
					const ctx = new OfflineAudioContext(2, frames, sr);
					const buffer = ctx.createBuffer(2, frames, sr);
					for (let c = 0; c < 2; c++) {
						const d = buffer.getChannelData(c);
						for (let i = 0; i < frames; i++) d[i] = Math.random() - 0.5;
					}
					const player = ctx.createBufferSource();
					player.buffer = buffer;
					player.start(0);
					if (withKernel) {
						const kernel = mod.buildBeatFxFilterKernel(ctx);
						player.connect(kernel.input);
						kernel.output.connect(ctx.destination);
						kernel.setClock(
							mod.resolveBeatFxFilterClock({
								beats: 1 / 16,
								timeMs: 22,
								grid: null,
								contextTime: 0
							}) /* 22 ms: about 1/16 beat at 174 BPM, the fastest LFO */,
							0
						);
						kernel.setLevel(1, 0);
					} else {
						const gain = ctx.createGain();
						player.connect(gain).connect(ctx.destination);
					}
					const t0 = performance.now();
					await ctx.startRendering();
					return performance.now() - t0;
				}
				const median = (xs: number[]) => xs.sort((a, b) => a - b)[1];
				const kernel: number[] = [];
				const bare: number[] = [];
				for (let r = 0; r < 3; r++) {
					kernel.push(await once(true));
					bare.push(await once(false));
				}
				return { kernelMs: median(kernel), bareMs: median(bare) };
			},
			{ sr: SR }
		);
		console.log(
			`[CPU] beat-fx-filter: 10 s stereo, kernel ${timing.kernelMs.toFixed(1)} ms, bare gain ${timing.bareMs.toFixed(1)} ms, ratio ${(timing.kernelMs / timing.bareMs).toFixed(2)}`
		);
		expect(timing.kernelMs).toBeGreaterThan(0);
	});
});
