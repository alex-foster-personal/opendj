/**
 * Crossfaded echo retune (FXECHO-03), rendered by a real Web Audio graph.
 *
 * The unit tests (tests/unit/echo-kernel.test.mjs) check the automation the
 * kernel schedules on a fake context. This spec renders the PRODUCTION kernel
 * (src/lib/rb/fx/echo-kernel.ts, with color-fx-graph's echo constants) through
 * Chromium's OfflineAudioContext and reads the samples back, so the two delay
 * lines, their gain ramps and the feedback loop are Chromium's own. Warm
 * retunes run inside ctx.suspend at whole render quanta and read ctx.currentTime,
 * as applyColorFxTargets does.
 *
 * Timeline at 124 BPM: cold start at 1/4 beat; division change to 1 beat at
 * quantum 263; retune to 3/4 beat at 1.2 s; replaced mid-crossfade by 1/2 beat
 * at quantum 454 (about 10.7 ms into the 3/4 fade).
 *
 * Two control subjects rebuild the behaviour the kernel must not have, so a
 * passing kernel run means something: the removed 0.05 s setTargetAtTime glide
 * (must fail the pitch bound) and a delayTime step on the live line (must fail
 * the click bound). Thresholds were measured Mon 9 Nov 2026 (values beside each).
 *
 * Runs under the root playwright.config.ts; it only visits about:blank.
 *
 * Requirements:
 *
 * - ✔︎ ✅ 🎯 The rendered line gains sum to 1 and move at most the 30 ms slope per quantum.
 * - ✔︎ ✅ 🎯 The cold start is a step; the division-change fade lasts 30 ms.
 * - ✔︎ ✅ 🎯 After a mid-crossfade replacement the echo emerges at the newest time.
 * - ✔︎ ✅ 🎯 Repeats keep their pitch through the real feedback loop.
 * - ✔︎ ✅ 🎯 A retune makes no per-sample step beyond the signal's own slope.
 *
 * Acceptance tests:
 *
 * - [if] a division change 1/4 to 1 beat at 124 BPM moves energy off the 1 kHz band [then ⛔️] the repeats pitch-slide.
 * - [if] a retune makes a per-sample step above the sine's own slope [then ⛔️] the retune clicks.
 * - [if] the two rendered line gains do not sum to 1 within 0.01, or either jumps beyond the 30 ms slope in a quantum [then ⛔️] a gain steps.
 * - [if] an impulse after the mid-crossfade replacement emerges at an older time [then ⛔️] an older time won.
 * - [if] line 1 is non-zero before the first warm retune [then ⛔️] the cold start crossfaded.
 * - [if] the glide control passes the pitch bound or the step control passes the click bound [then ⛔️] the detector is blind.
 */
import { expect, test, type Page } from '@playwright/test';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SR = 48_000;
const QUANTUM = 128;
const RENDER_SEC = 2.0;
const BEAT = 60 / 124;
const XFADE_SEC = 0.03;
/** [time (s), new echo time (s)]; each time is a whole render quantum. */
const RETUNES: [number, number][] = [
	[(263 * QUANTUM) / SR, BEAT],
	[(450 * QUANTUM) / SR, 0.75 * BEAT],
	[(454 * QUANTUM) / SR, 0.5 * BEAT]
];
const SETTLED_SEC = 1.3;
/** Pitch bounds, measured Mon 9 Nov 2026: kernel aggregate 0.99995, worst frame 0.937; glide control 0.678, worst frame 1.7e-9. */
const PITCH_AGGREGATE_MIN = 0.9;
const PITCH_FRAME_MIN = 0.8;
/** Click bound: a 440 Hz sine at 0.5 moves at most 2*pi*440*0.5/SR per sample (measured: kernel 0.0291, step control 0.858). */
const SINE_SLOPE = (2 * Math.PI * 440 * 0.5) / SR;
const CLICK_MAX = 1.2 * SINE_SLOPE;
/** Sub-knee DC matching `ECHO_GAIN_PROBE_DC` in echo-kernel.ts (FXECHO-10612 identity region). */
const GAIN_PROBE_DC = 10 ** (-6 / 20) * 0.99;

type Subject = 'kernel' | 'control-glide' | 'control-step';
type Signal = 'dc' | 'impulses' | 'bursts' | 'sine';
/** The esbuild bundle's exports, typed from the real modules (no escape hatch). */
type EchoBundle = typeof import('../../src/lib/rb/fx/echo-kernel') &
	Pick<
		typeof import('../../src/lib/rb/color-fx-graph'),
		'COLOR_FX_ECHO_MAX_DELAY_SEC' | 'ECHO_FEEDBACK' | 'ECHO_LOOP_HP_HZ' | 'ECHO_LOOP_LP_HZ'
	>;

async function echoBundle(): Promise<string> {
	const result = await build({
		stdin: {
			contents: [
				"export * from './src/lib/rb/fx/echo-kernel';",
				"export { COLOR_FX_ECHO_MAX_DELAY_SEC, ECHO_FEEDBACK, ECHO_LOOP_HP_HZ, ECHO_LOOP_LP_HZ } from './src/lib/rb/color-fx-graph';"
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

/** Render one subject; returns channels [output, lineGain0 tap, lineGain1 tap] (taps kernel only). */
async function render(
	page: Page,
	opts: { subject: Subject; signal: Signal; loop: boolean }
): Promise<number[][]> {
	return page.evaluate(
		async ({ subject, signal, loop, sr, seconds, beat, retunes, gainProbeDc }) => {
			const mod = (globalThis as unknown as { echoKernel: EchoBundle }).echoKernel;
			const frames = Math.round(sr * seconds);
			const ctx = new OfflineAudioContext(3, frames, sr);
			const feedbackGain = loop ? mod.ECHO_FEEDBACK : 0;

			let source: AudioNode;
			if (signal === 'dc') {
				const dc = ctx.createConstantSource();
				dc.offset.value = gainProbeDc;
				dc.start(0);
				source = dc;
			} else if (signal === 'sine') {
				const osc = ctx.createOscillator();
				osc.frequency.value = 440;
				const amp = ctx.createGain();
				amp.gain.value = 0.5;
				osc.connect(amp);
				osc.start(0);
				source = amp;
			} else {
				const buffer = ctx.createBuffer(1, frames, sr);
				const data = buffer.getChannelData(0);
				if (signal === 'impulses') {
					for (const t of [0.05, 1.4]) data[Math.round(t * sr)] = 1;
				} else {
					const burstLen = Math.round(0.04 * sr);
					for (const t of [0.02, 0.55, 1.05, 1.15]) {
						const at = Math.round(t * sr);
						for (let i = 0; i < burstLen; i++) {
							const hann = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (burstLen - 1));
							data[at + i] += 0.5 * hann * Math.sin((2 * Math.PI * 1000 * i) / sr);
						}
					}
				}
				const player = ctx.createBufferSource();
				player.buffer = buffer;
				player.start(0);
				source = player;
			}

			const merger = ctx.createChannelMerger(3);
			merger.connect(ctx.destination);
			const coldSec = beat / 4;
			let retune: (seconds: number, now: number) => void;

			if (subject === 'kernel') {
				const kernel = mod.buildEchoKernel(ctx, {
					mode: 'echo',
					maxDelaySec: mod.COLOR_FX_ECHO_MAX_DELAY_SEC,
					feedback: feedbackGain,
					loopHpHz: mod.ECHO_LOOP_HP_HZ,
					loopLpHz: mod.ECHO_LOOP_LP_HZ
				});
				source.connect(kernel.input);
				kernel.output.connect(merger, 0, 0);
				kernel.lineGains[0].connect(merger, 0, 1);
				kernel.lineGains[1].connect(merger, 0, 2);
				kernel.setTime(coldSec, 0);
				retune = (s, now) => kernel.setTime(s, now);
			} else {
				// The behaviour the kernel replaced: one line, retimed in place.
				const sum = ctx.createGain();
				const line = ctx.createDelay(mod.COLOR_FX_ECHO_MAX_DELAY_SEC);
				const hp = ctx.createBiquadFilter();
				hp.type = 'highpass';
				hp.frequency.value = mod.ECHO_LOOP_HP_HZ;
				const lp = ctx.createBiquadFilter();
				lp.type = 'lowpass';
				lp.frequency.value = mod.ECHO_LOOP_LP_HZ;
				const fb = ctx.createGain();
				fb.gain.value = feedbackGain;
				source.connect(sum);
				sum.connect(line);
				line.connect(hp);
				hp.connect(lp);
				lp.connect(fb);
				fb.connect(sum);
				line.connect(merger, 0, 0);
				line.delayTime.setValueAtTime(coldSec, 0);
				retune =
					subject === 'control-glide'
						? (s, now) => line.delayTime.setTargetAtTime(s, now, 0.05)
						: (s, now) => line.delayTime.setValueAtTime(s, now);
			}

			for (const [at, s] of retunes) {
				ctx.suspend(at).then(() => {
					retune(s, ctx.currentTime);
					ctx.resume();
				});
			}
			const rendered = await ctx.startRendering();
			return [0, 1, 2].map((c) => Array.from(rendered.getChannelData(c)));
		},
		{
			...opts,
			sr: SR,
			seconds: RENDER_SEC,
			beat: BEAT,
			retunes: RETUNES,
			gainProbeDc: GAIN_PROBE_DC
		}
	);
}

//----- metrics

const frameAt = (seconds: number): number => Math.round(seconds * SR);

function firstOnsetAfter(x: number[], seconds: number, floor = 1e-4): number {
	for (let i = frameAt(seconds) + 1; i < x.length; i++) if (Math.abs(x[i]) > floor) return i;
	return -1;
}

function maxStep(x: number[], fromSec: number): number {
	let worst = 0;
	for (let i = frameAt(fromSec); i < x.length; i++)
		worst = Math.max(worst, Math.abs(x[i] - x[i - 1]));
	return worst;
}

/** Share of energy in 900-1100 Hz: aggregate and worst loud frame, Hann 2048 / hop 512. */
function bandRatio(x: number[], fromSec: number): { aggregate: number; worstFrame: number } {
	const n = 2048;
	const hop = 512;
	const hann = Array.from(
		{ length: n },
		(_, i) => 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1))
	);
	const kLo = Math.ceil((900 * n) / SR);
	const kHi = Math.floor((1100 * n) / SR);
	let band = 0;
	let total = 0;
	let worstFrame = 1;
	for (let start = frameAt(fromSec); start + n <= x.length; start += hop) {
		const w = hann.map((h, i) => h * x[start + i]);
		const sumSq = w.reduce((a, v) => a + v * v, 0);
		if (Math.sqrt(sumSq / n) < 1e-3) continue;
		let frameBand = 0;
		for (let k = kLo; k <= kHi; k++) {
			let re = 0;
			let im = 0;
			for (let i = 0; i < n; i++) {
				const phase = (2 * Math.PI * k * i) / n;
				re += w[i] * Math.cos(phase);
				im -= w[i] * Math.sin(phase);
			}
			frameBand += 2 * (re * re + im * im);
		}
		const frameTotal = n * sumSq;
		band += frameBand;
		total += frameTotal;
		worstFrame = Math.min(worstFrame, frameBand / frameTotal);
	}
	return { aggregate: band / total, worstFrame };
}

//----- tests

test.describe('echo retune through a real OfflineAudioContext (FXECHO-03)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('about:blank');
		await page.addScriptTag({ content: await echoBundle() });
	});

	test('rendered line gains: cold step, 30 ms fade, sum 1, slope-bounded per quantum', async ({
		page
	}) => {
		const [out, g0, g1] = await render(page, { subject: 'kernel', signal: 'dc', loop: false });
		const from = frameAt(0.13);
		const slopeBound = QUANTUM / (XFADE_SEC * SR) + 0.01;
		let worstSum = 0;
		let worstOut = 0;
		let worstQuantumStep = 0;
		for (let i = from; i < out.length; i++) {
			worstSum = Math.max(worstSum, Math.abs(g0[i] + g1[i] - GAIN_PROBE_DC));
			worstOut = Math.max(worstOut, Math.abs(out[i] - (g0[i] + g1[i])));
			if (i % QUANTUM === 0 && i >= from + QUANTUM) {
				worstQuantumStep = Math.max(
					worstQuantumStep,
					Math.abs(g0[i] - g0[i - QUANTUM]),
					Math.abs(g1[i] - g1[i - QUANTUM])
				);
			}
		}
		console.log(
			`gains: sum err ${worstSum}, out err ${worstOut}, quantum step ${worstQuantumStep}`
		);
		expect(worstSum).toBeLessThanOrEqual(0.01);
		expect(worstOut).toBeLessThanOrEqual(1e-5);
		expect(worstQuantumStep).toBeLessThanOrEqual(slopeBound);

		let coldErr = 0;
		for (let i = from; i < frameAt(0.7); i++)
			coldErr = Math.max(coldErr, Math.abs(g0[i] - GAIN_PROBE_DC), Math.abs(g1[i]));
		expect(coldErr).toBeLessThanOrEqual(1e-6);
		let fading = 0;
		const fadeLo = 0.001 * GAIN_PROBE_DC;
		const fadeHi = 0.999 * GAIN_PROBE_DC;
		for (let i = frameAt(0.7); i < frameAt(0.8); i++) {
			if (g0[i] > fadeLo && g0[i] < fadeHi && g1[i] > fadeLo && g1[i] < fadeHi) fading++;
		}
		console.log(`division-change fade frames: ${fading}`);
		expect(Math.abs(fading - XFADE_SEC * SR)).toBeLessThanOrEqual(QUANTUM);
		let settledErr = 0;
		for (let i = frameAt(SETTLED_SEC); i < out.length; i++) {
			settledErr = Math.max(
				settledErr,
				Math.min(Math.abs(g0[i] - GAIN_PROBE_DC), Math.abs(g1[i] - GAIN_PROBE_DC)),
				Math.min(Math.abs(g0[i]), Math.abs(g1[i]))
			);
		}
		expect(settledErr).toBeLessThanOrEqual(1e-6);
	});

	test('impulses emerge at the cold time, then at the newest time after a mid-crossfade replacement', async ({
		page
	}) => {
		const [out] = await render(page, { subject: 'kernel', signal: 'impulses', loop: false });
		expect(Math.abs(firstOnsetAfter(out, 0.05) - (0.05 + BEAT / 4) * SR)).toBeLessThanOrEqual(1);
		expect(Math.abs(firstOnsetAfter(out, 1.4) - (1.4 + 0.5 * BEAT) * SR)).toBeLessThanOrEqual(1);
		for (const older of [0.75 * BEAT, BEAT]) {
			const at = frameAt(1.4 + older);
			const near = out.slice(at - 2, at + 3).map(Math.abs);
			expect(Math.max(...near)).toBeLessThan(1e-6);
		}
	});

	test('repeats keep their pitch through the feedback loop; the glide control does not', async ({
		page
	}) => {
		const [kernelOut] = await render(page, { subject: 'kernel', signal: 'bursts', loop: true });
		const [glideOut] = await render(page, {
			subject: 'control-glide',
			signal: 'bursts',
			loop: true
		});
		const kernel = bandRatio(kernelOut, 0.6);
		const glide = bandRatio(glideOut, 0.6);
		console.log(`pitch: kernel ${JSON.stringify(kernel)}, glide control ${JSON.stringify(glide)}`);
		expect(kernel.aggregate).toBeGreaterThanOrEqual(PITCH_AGGREGATE_MIN);
		expect(kernel.worstFrame).toBeGreaterThanOrEqual(PITCH_FRAME_MIN);
		expect(glide.aggregate).toBeLessThan(PITCH_AGGREGATE_MIN);
		expect(glide.worstFrame).toBeLessThan(PITCH_FRAME_MIN);
	});

	test('a retune adds no per-sample step beyond the sine slope; the step control does', async ({
		page
	}) => {
		const [kernelOut] = await render(page, { subject: 'kernel', signal: 'sine', loop: false });
		const [stepOut] = await render(page, { subject: 'control-step', signal: 'sine', loop: false });
		const kernel = maxStep(kernelOut, 0.13);
		const step = maxStep(stepOut, 0.13);
		console.log(`click: kernel ${kernel}, step control ${step}, bound ${CLICK_MAX}`);
		expect(kernel).toBeLessThanOrEqual(CLICK_MAX);
		expect(step).toBeGreaterThan(CLICK_MAX);
	});
});
