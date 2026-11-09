// requirement: FXECHO-10612
/**
 * Echo loop headroom guard (FXECHO-10612), rendered by a real Web Audio graph.
 *
 * The unit simulator (tests/unit/echo-render.test.mjs) is fast supplementary
 * coverage for timing and tail. This spec renders the PRODUCTION kernel through
 * Chromium's OfflineAudioContext so the WaveShaper, both DelayNodes, gains and
 * feedback cycle are Chromium's own.
 *
 * Subject `bypass` rewires the production kernel: input disconnects softClip, then
 * input feeds both delay lines directly (pre-guard behaviour). If the shaper were
 * missing in production, the guard matrix would still fail on that control.
 *
 * Thresholds measured Mon 9 Nov 2026: bypass outPeak 1.306; guard worst outPeak 0.840, hot loopPeak
 * 1.225; quiet max diff 4.6e-7 (1 beat) and 5.6e-7 (1/4 beat).
 *
 * Runs under the root playwright.config.ts; it only visits about:blank.
 *
 * Requirements:
 *
 * - ✔︎ ✅ 🎯 At feedback 0.95 no output sample exceeds ECHO_LOOP_CEILING on any fixture, division or retune.
 * - ✔︎ ✅ 🎯 Below -6 dBFS the guarded render matches bypass within 1e-6.
 * - ✔︎ ✅ 🎯 Bypass at 0.95 exceeds 0 dBFS so the detector bites when the shaper is absent.
 *
 * Acceptance tests:
 *
 * - [if] bypass runs at feedback 0.95 on hot bursts [then ⛔️] outPeak is not above 1.
 * - [if] kernel runs at feedback 0.95 on any fixture or schedule [then ⛔️] outPeak exceeds the ceiling.
 * - [if] hot bursts at 1 beat on kernel [then ⛔️] loopPeak does not exceed the knee.
 * - [if] quiet bursts run on kernel and bypass at feedback 0.55 [then ⛔️] max abs diff exceeds 1e-6.
 */
import { expect, test, type Page } from '@playwright/test';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SR = 48_000;
const QUANTUM = 128;
const BEAT = 60 / 124;
const DIVISIONS = [0.25, 0.5, 0.75, 1] as const;
const CEILING_SLACK = 1e-6;

type Subject = 'kernel' | 'bypass';
type Fixture = 'bursts' | 'sine' | 'noise';

type RenderOpts = {
	subject: Subject;
	fixture: Fixture;
	feedback: number;
	divisionBeats: number;
	seconds: number;
	burstPeak?: number;
	retune?: boolean;
	keepOutput?: boolean;
};

type RenderResult = {
	outPeak: number;
	outPeakIdx: number;
	loopPeak: number;
	output?: number[];
};

async function echoBundle(): Promise<string> {
	const result = await build({
		stdin: {
			contents: [
				"export * from './src/lib/rb/fx/echo-kernel';",
				"export { COLOR_FX_ECHO_MAX_DELAY_SEC, ECHO_LOOP_HP_HZ, ECHO_LOOP_LP_HZ } from './src/lib/rb/color-fx-graph';"
			].join('\n'),
			resolveDir: FRONTEND_ROOT,
			loader: 'ts'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: `${FRONTEND_ROOT}/src/lib` },
		bundle: true,
		format: 'iife',
		globalName: 'echoKernel',
		platform: 'browser',
		write: false,
		logLevel: 'silent'
	});
	return result.outputFiles[0].text;
}

async function render(page: Page, opts: RenderOpts): Promise<RenderResult> {
	return page.evaluate(
		async ({
			subject,
			fixture,
			feedback,
			divisionBeats,
			seconds,
			burstPeak,
			retune,
			keepOutput,
			sr,
			beat,
			quantum
		}) => {
			// eslint-disable-next-line @typescript-eslint/no-explicit-any
			const mod = (globalThis as any).echoKernel;
			const frames = Math.round(sr * seconds);
			const ctx = new OfflineAudioContext(2, frames, sr);
			const buffer = ctx.createBuffer(1, frames, sr);
			const data = buffer.getChannelData(0);

			if (fixture === 'bursts') {
				const peak = burstPeak ?? 1;
				const beatSamples = Math.round(beat * sr);
				const burstLen = Math.round(0.08 * sr);
				for (let s = 0; s < frames; s += beatSamples) {
					for (let i = 0; i < burstLen && s + i < frames; i++) {
						data[s + i] +=
							peak *
							Math.exp(-i / (0.03 * sr)) *
							Math.sin((2 * Math.PI * 1000 * i) / sr + Math.PI / 2);
					}
				}
			} else if (fixture === 'sine') {
				const len = Math.round(0.2 * sr);
				for (let i = 0; i < len; i++) data[i] = Math.sin((2 * Math.PI * 1000 * i) / sr);
			} else {
				const len = Math.round(0.2 * sr);
				let seed = 12345;
				for (let i = 0; i < len; i++) {
					seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
					data[i] = seed / 2 ** 31 - 1;
				}
			}

			const player = ctx.createBufferSource();
			player.buffer = buffer;
			player.start(0);

			const kernel = mod.buildEchoKernel(ctx, {
				maxDelaySec: mod.COLOR_FX_ECHO_MAX_DELAY_SEC,
				feedback,
				loopHpHz: mod.ECHO_LOOP_HP_HZ,
				loopLpHz: mod.ECHO_LOOP_LP_HZ
			});

			if (subject === 'kernel' && kernel.softClip.curve?.length !== mod.ECHO_LOOP_CURVE_LENGTH) {
				throw new Error('production softClip curve not installed');
			}

			player.connect(kernel.input);

			if (subject === 'bypass') {
				kernel.input.disconnect(kernel.softClip);
				kernel.softClip.disconnect();
				kernel.input.connect(kernel.lines[0]);
				kernel.input.connect(kernel.lines[1]);
			}

			const merger = ctx.createChannelMerger(2);
			merger.connect(ctx.destination);
			kernel.output.connect(merger, 0, 0);
			kernel.input.connect(merger, 0, 1);

			kernel.setTime(divisionBeats * beat, 0);

			if (retune) {
				const t = (Math.round((1.5 * sr) / quantum) * quantum) / sr;
				ctx.suspend(t).then(() => {
					kernel.setTime(beat, ctx.currentTime);
					ctx.resume();
				});
			}

			const rendered = await ctx.startRendering();
			const out = rendered.getChannelData(0);
			const loop = rendered.getChannelData(1);
			let outPeak = 0;
			let outPeakIdx = 0;
			let loopPeak = 0;
			for (let i = 0; i < out.length; i++) {
				const ao = Math.abs(out[i]);
				if (ao > outPeak) {
					outPeak = ao;
					outPeakIdx = i;
				}
				loopPeak = Math.max(loopPeak, Math.abs(loop[i]));
			}
			const result: RenderResult = { outPeak, outPeakIdx, loopPeak };
			if (keepOutput) result.output = Array.from(out);
			return result;
		},
		{
			...opts,
			sr: SR,
			beat: BEAT,
			quantum: QUANTUM
		}
	);
}

test.describe('echo headroom through a real OfflineAudioContext (FXECHO-10612)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('about:blank');
		await page.addScriptTag({ content: await echoBundle() });
	});

	test('known failure: bypass at feedback 0.95 exceeds 0 dBFS on hot bursts', async ({ page }) => {
		const { outPeak } = await render(page, {
			subject: 'bypass',
			fixture: 'bursts',
			feedback: 0.95,
			divisionBeats: 1,
			seconds: 3,
			burstPeak: 1
		});
		console.log(`bypass known failure outPeak ${outPeak}`);
		expect(outPeak).toBeGreaterThan(1);
	});

	test('guard: no output over the ceiling on every fixture, division and retune at 0.95', async ({
		page
	}) => {
		const fixtures: { name: Fixture; burstPeak?: number }[] = [
			{ name: 'bursts', burstPeak: 1 },
			{ name: 'sine' },
			{ name: 'noise' }
		];
		const schedules: { label: string; divisionBeats: number; retune?: boolean }[] = DIVISIONS.map(
			(b) => ({ label: `${b} beat`, divisionBeats: b })
		);
		schedules.push({ label: 'retune 1/4 to 1', divisionBeats: 0.25, retune: true });

		let worst = 0;
		let kneeChecked = false;

		for (const { name, burstPeak } of fixtures) {
			for (const { label, divisionBeats, retune } of schedules) {
				const r = await render(page, {
					subject: 'kernel',
					fixture: name,
					feedback: 0.95,
					divisionBeats,
					seconds: 3,
					...(burstPeak !== undefined ? { burstPeak } : {}),
					...(retune !== undefined ? { retune } : {})
				});
				worst = Math.max(worst, r.outPeak);
				expect(
					r.outPeak,
					`${name} ${label}: peak ${r.outPeak} at sample ${r.outPeakIdx}`
				).toBeLessThanOrEqual(0.9 + CEILING_SLACK);
			}
			if (name === 'bursts') {
				const hot = await render(page, {
					subject: 'kernel',
					fixture: 'bursts',
					feedback: 0.95,
					divisionBeats: 1,
					seconds: 3,
					burstPeak: 1
				});
				console.log(`guard bursts 1 beat loopPeak ${hot.loopPeak}, outPeak ${hot.outPeak}`);
				expect(hot.loopPeak).toBeGreaterThan(0.501);
				kneeChecked = true;
			}
		}
		expect(kneeChecked).toBe(true);
		console.log(`guard worst outPeak ${worst}`);
	});

	test('quiet identity: guarded matches bypass within 1e-6 below the knee', async ({ page }) => {
		const args = {
			fixture: 'bursts' as const,
			feedback: 0.55,
			seconds: 3,
			burstPeak: 0.1
		};
		for (const divisionBeats of [1, 0.25] as const) {
			const bypass = await render(page, {
				...args,
				subject: 'bypass',
				divisionBeats,
				keepOutput: true
			});
			expect(bypass.loopPeak).toBeLessThan(0.501);
			const kernel = await render(page, {
				...args,
				subject: 'kernel',
				divisionBeats,
				keepOutput: true
			});
			expect(kernel.outPeak).toBeGreaterThan(0.05);
			let diff = 0;
			const a = bypass.output!;
			const b = kernel.output!;
			for (let i = 0; i < a.length; i++) diff = Math.max(diff, Math.abs(a[i] - b[i]));
			console.log(`quiet identity division ${divisionBeats} max diff ${diff}`);
			expect(diff).toBeLessThan(1e-6);
		}
	});
});
