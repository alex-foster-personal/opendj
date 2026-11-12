/**
 * Where the color send sits (COLORFX-10754), rendered by a real Web Audio graph.
 *
 * tests/unit/color-fx-placement.test.mjs pins the production wiring on a fake
 * context. This spec renders the PRODUCTION color FX splice
 * (src/lib/rb/color-fx-graph.ts, driveColorChannel) through Chromium's
 * OfflineAudioContext and reads the samples back. The channel tail around it is
 * a replica of deck-channel-graph.ts (stem sum -> trim -> EQ -> FILTER ->
 * colorBus -> cue / fader -> xf -> master): that module imports Svelte rune
 * modules and cannot be bundled into the page, so the unit test pins the real
 * edges and this replica copies them.
 *
 * ECHO at 1/2 beat, 124 BPM (0.2419 s), knob fully clockwise, driven at t = 0.
 * Source: 40 ms Hann-windowed 1 kHz bursts at 0.5, so repeats are discrete.
 *
 * Fader moves: the deck-move handover steps the fader (setValueAtTime) and the
 * knob ramps it with setTargetAtTime at PARAM_SMOOTH_S (10 ms). A 10 ms time
 * constant reaches -90 dB only after about 10.4 tau (104 ms), so the 20 ms
 * bound holds for the step cut; the knob ramp is checked as "master is the
 * pre-fader signal times the fader law" and below -90 dB by 110 ms.
 *
 * A control splice returns the wet signal past the fader (into xf) and must
 * fail the step-cut bound, so a passing run means the detector can see a
 * post-fader return.
 *
 * Runs under tests/e2e/playwright.color-fx-placement.config.ts (no dev server);
 * it only visits about:blank. Fold it
 * into tests/e2e/fx-render.spec.ts once the shared render harness (#9774) lands.
 *
 * Requirements:
 *
 * - ✔︎ ✅ 🎯 A channel fader step cut takes the channel's master contribution, wet tail included, below -90 dB within 20 ms.
 * - ✔︎ ✅ 🎯 On the fader knob ramp the master is the pre-fader signal times the fader law, below -90 dB by 110 ms.
 * - ✔︎ ✅ 🎯 A muted stem sends nothing new into the echo after its 10 ms ramp, and the tail rings on for at least 2 repeats.
 * - ✔︎ ✅ 🎯 With the cue on and the fader at 0, the cue sum carries the wet signal and the master is silent.
 *
 * Acceptance tests:
 *
 * - [if] the master is above -90 dB re its pre-cut peak 20 ms after a fader step cut while the echo still rings [then ⛔️] the wet tail bypasses the fader.
 * - [if] after a fader knob cut the master departs from pre-fader x exp(-t/10 ms) [then ⛔️] something reaches the master around the fader.
 * - [if] the echo input carries anything above -90 dB re its pre-mute peak 110 ms after a stem mute [then ⛔️] a muted stem still feeds the echo.
 * - [if] fewer than 2 echo repeats of the last burst follow the stem mute [then ⛔️] the mute cut the tail.
 * - [if] the cue sum lacks the echo repeats with the fader at 0, or the master is not exactly 0 [then ⛔️] the cue is not a pre-fader tap after the color FX.
 * - [if] the post-fader control passes the step-cut bound, or the unmuted control shows no later echo input [then ⛔️] the detector is blind.
 */
import { expect, test, type Page } from '@playwright/test';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SR = 48_000;
const QUANTUM = 128;
const RENDER_SEC = 2.5;
const BPM = 124;
const ECHO_SEC = 0.5 * (60 / BPM);
const BURST_SEC = 0.04;
/** Output channels of the render. */
const TAP = { master: 0, cueSum: 1, echoIn: 2, echoReturn: 3, preFader: 4 } as const;
/** Bounds (dB re the reference named at each use). */
const CUT_FLOOR_DB = -90;
const STEP_CUT_SEC = 0.02;
const KNOB_CUT_SEC = 0.11;
/** Knob-law error bound, as a fraction of the pre-fader peak. */
const KNOB_LAW_TOL = 1e-3;
/** A repeat counts when its peak is within this of the first repeat (measured about -5 dB a repeat). */
const REPEAT_FLOOR_DB = -20;
/** The controls must stay above this. */
const CONTROL_FLOOR_DB = -40;
/** Worst |cueSum - echoReturn| allowed after the dry ends. */
const CUE_WET_TOL = 1e-5;

type Bundle = Pick<typeof import('../../src/lib/rb/color-fx-graph'), 'driveColorChannel'> &
	Pick<typeof import('../../src/lib/player/constants'), 'PARAM_SMOOTH_S'>;

interface RenderOpts {
	bursts: number[];
	faderStart: number;
	cut: { kind: 'step' | 'knob'; at: number } | null;
	stemMuteAt: number | null;
	cueOn: boolean;
	postFader: boolean;
}

async function bundle(): Promise<string> {
	const result = await build({
		stdin: {
			contents: [
				"export { driveColorChannel } from './src/lib/rb/color-fx-graph';",
				"export { PARAM_SMOOTH_S } from './src/lib/player/constants';"
			].join('\n'),
			resolveDir: FRONTEND_ROOT,
			loader: 'ts'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: `${FRONTEND_ROOT}/src/lib` },
		bundle: true,
		format: 'iife',
		globalName: 'colorFxGraph',
		platform: 'browser',
		write: false,
		logLevel: 'silent'
	});
	return result.outputFiles[0].text;
}

/** Render one channel; returns the TAP channels. */
async function render(page: Page, opts: RenderOpts): Promise<number[][]> {
	return page.evaluate(
		async ({ opts, sr, seconds, bpm, burstSec, taps }) => {
			const mod = (globalThis as unknown as { colorFxGraph: Bundle }).colorFxGraph;
			const frames = Math.round(sr * seconds);
			const ctx = new OfflineAudioContext(5, frames, sr);
			ctx.destination.channelInterpretation = 'discrete';
			const gain = (v: number): GainNode => {
				const g = ctx.createGain();
				g.gain.value = v;
				return g;
			};
			const biquad = (type: BiquadFilterType, hz: number): BiquadFilterNode => {
				const b = ctx.createBiquadFilter();
				b.type = type;
				b.frequency.value = hz;
				return b;
			};

			// Two stem parts summed into the deck input: A carries the bursts, B is silent.
			const partA = ctx.createBuffer(1, frames, sr);
			const data = partA.getChannelData(0);
			const burstLen = Math.round(burstSec * sr);
			for (const t of opts.bursts) {
				const at = Math.round(t * sr);
				for (let i = 0; i < burstLen && at + i < frames; i++) {
					const hann = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (burstLen - 1));
					data[at + i] += 0.5 * hann * Math.sin((2 * Math.PI * 1000 * i) / sr);
				}
			}
			const stemSum = gain(1);
			const partGains: GainNode[] = [];
			for (const buffer of [partA, ctx.createBuffer(1, frames, sr)]) {
				const src = ctx.createBufferSource();
				src.buffer = buffer;
				const partGain = gain(1);
				src.connect(partGain).connect(stemSum);
				src.start(0);
				partGains.push(partGain);
			}

			// Replica of deck-channel-graph.ts:114-135 at unity trim, flat EQ, FILTER bypass.
			const trim = gain(1);
			const low = biquad('lowshelf', 250);
			const mid = biquad('peaking', 1200);
			const high = biquad('highshelf', 5000);
			const filterLp = biquad('lowpass', 20000);
			const filterHp = biquad('highpass', 20);
			const filterDry = gain(1);
			const filterLpWet = gain(0);
			const filterHpWet = gain(0);
			const colorBus = gain(1);
			const cue = gain(opts.cueOn ? 1 : 0);
			const fader = gain(opts.faderStart);
			const xf = gain(1);
			const masterGain = gain(1);
			const cueSum = gain(1);
			stemSum.connect(trim).connect(low).connect(mid).connect(high);
			for (const stage of [filterDry, filterLp, filterHp]) high.connect(stage);
			filterLp.connect(filterLpWet);
			filterHp.connect(filterHpWet);
			for (const branch of [filterDry, filterLpWet, filterHpWet]) branch.connect(colorBus);
			colorBus.connect(cue).connect(cueSum);
			colorBus.connect(fader).connect(xf).connect(masterGain);

			// The production splice. The control hands it xf as "the fader", so
			// the returns land past the real fader.
			const rt = {
				nodes: {
					high,
					filterLp,
					filterHp,
					filterDry,
					filterLpWet,
					filterHpWet,
					colorBus,
					cue,
					fader: opts.postFader ? xf : fader
				},
				graph: null,
				crush: null,
				spaceQuietSince: null
			};
			mod.driveColorChannel(rt, { effect: 'echo', echo_beats: 0.5 }, bpm, 1, 0.5);
			const fx = rt.graph as unknown as {
				echoIn: GainNode;
				echoReturn: GainNode;
				spaceReturn: GainNode;
			};

			const preFader = gain(1);
			for (const n of [colorBus, fx.echoReturn, fx.spaceReturn]) n.connect(preFader);
			const merger = ctx.createChannelMerger(5);
			masterGain.connect(merger, 0, taps.master);
			cueSum.connect(merger, 0, taps.cueSum);
			fx.echoIn.connect(merger, 0, taps.echoIn);
			fx.echoReturn.connect(merger, 0, taps.echoReturn);
			preFader.connect(merger, 0, taps.preFader);
			merger.connect(ctx.destination);

			if (opts.cut !== null && opts.cut.kind === 'step') fader.gain.setValueAtTime(0, opts.cut.at);
			else if (opts.cut !== null && opts.cut.kind === 'knob')
				fader.gain.setTargetAtTime(0, opts.cut.at, mod.PARAM_SMOOTH_S);
			if (opts.stemMuteAt !== null) partGains[0].gain.setTargetAtTime(0, opts.stemMuteAt, 0.01);

			const rendered = await ctx.startRendering();
			return Array.from({ length: 5 }, (_, ch) => Array.from(rendered.getChannelData(ch)));
		},
		{ opts, sr: SR, seconds: RENDER_SEC, bpm: BPM, burstSec: BURST_SEC, taps: TAP }
	);
}

//----- analysis helpers

const frameAt = (sec: number): number => Math.round(sec * SR);
/** The whole render quantum nearest `sec`, in seconds. */
const quantumAt = (sec: number): number => (Math.round((sec * SR) / QUANTUM) * QUANTUM) / SR;

function peak(x: number[], fromSec: number, toSec: number): number {
	let p = 0;
	for (let i = Math.max(0, frameAt(fromSec)); i < Math.min(x.length, frameAt(toSec)); i++)
		p = Math.max(p, Math.abs(x[i]));
	return p;
}
const db = (v: number, ref: number): number => 20 * Math.log10(Math.max(v, 1e-30) / ref);

/** Peaks of the repeats of a burst starting at `burstSec`, for repeats whose window starts after `afterSec`. */
function repeatPeaks(x: number[], burstSec: number, afterSec: number, count: number): number[] {
	const out: number[] = [];
	for (let k = 1; out.length < count; k++) {
		const start = burstSec + k * ECHO_SEC;
		if (start < afterSec) continue;
		out.push(peak(x, start - 0.01, start + BURST_SEC + 0.01));
	}
	return out;
}

const RENDER_END = RENDER_SEC;
const NO_CUT: Omit<RenderOpts, 'bursts'> = {
	faderStart: 1,
	cut: null,
	stemMuteAt: null,
	cueOn: false,
	postFader: false
};

//----- tests

test.describe('color FX placement through a real OfflineAudioContext (COLORFX-10754)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('about:blank');
		await page.addScriptTag({ content: await bundle() });
	});

	test('a fader step cut takes the wet tail below -90 dB within 20 ms; a post-fader return does not', async ({
		page
	}) => {
		const tc = quantumAt(0.8);
		const opts: RenderOpts = { ...NO_CUT, bursts: [0.05, 0.3], cut: { kind: 'step', at: tc } };
		const out = await render(page, opts);
		const ref = peak(out[TAP.master], 0, tc);
		const after = peak(out[TAP.master], tc + STEP_CUT_SEC, RENDER_END);
		expect(db(after, ref)).toBeLessThan(CUT_FLOOR_DB);
		// The tail still rings before the fader: the fader cut it, not the echo.
		expect(db(peak(out[TAP.preFader], tc + 0.1, RENDER_END), ref)).toBeGreaterThan(-30);

		const control = await render(page, { ...opts, postFader: true });
		const controlRef = peak(control[TAP.master], 0, tc);
		expect(
			db(peak(control[TAP.master], tc + STEP_CUT_SEC, RENDER_END), controlRef)
		).toBeGreaterThan(CONTROL_FLOOR_DB);
	});

	test('on the fader knob ramp the master is the pre-fader signal times the fader law', async ({
		page
	}) => {
		const tc = quantumAt(0.8);
		const out = await render(page, {
			...NO_CUT,
			bursts: [0.05, 0.3],
			cut: { kind: 'knob', at: tc }
		});
		const master = out[TAP.master];
		const pre = out[TAP.preFader];
		const preRef = peak(pre, 0, RENDER_END);
		let worst = 0;
		for (let i = frameAt(tc); i < master.length; i++) {
			const law = Math.exp(-(i / SR - tc) / 0.01);
			worst = Math.max(worst, Math.abs(master[i] - pre[i] * law));
		}
		expect(worst).toBeLessThanOrEqual(KNOB_LAW_TOL * preRef);
		const ref = peak(master, 0, tc);
		expect(db(peak(master, tc + KNOB_CUT_SEC, RENDER_END), ref)).toBeLessThan(CUT_FLOOR_DB);
	});

	test('a muted stem sends nothing new into the echo, and the tail rings on', async ({ page }) => {
		const bursts = Array.from({ length: 10 }, (_, k) => 0.05 + 0.25 * k);
		const tm = quantumAt(0.6);
		const out = await render(page, { ...NO_CUT, bursts, stemMuteAt: tm });
		const inRef = peak(out[TAP.echoIn], 0, tm);
		// A 10 ms ramp reaches -90 dB after about 104 ms (10.4 tau).
		expect(db(peak(out[TAP.echoIn], tm + 0.11, RENDER_END), inRef)).toBeLessThan(CUT_FLOOR_DB);
		const lastBurst = bursts.filter((t) => t < tm).at(-1) ?? 0;
		const firstRepeat = peak(
			out[TAP.echoReturn],
			lastBurst + ECHO_SEC - 0.01,
			lastBurst + ECHO_SEC + BURST_SEC + 0.01
		);
		const [r1, r2] = repeatPeaks(out[TAP.echoReturn], lastBurst, tm + 0.11, 2);
		expect(db(r1, firstRepeat)).toBeGreaterThan(REPEAT_FLOOR_DB);
		expect(db(r2, firstRepeat)).toBeGreaterThan(REPEAT_FLOOR_DB);

		const control = await render(page, { ...NO_CUT, bursts });
		expect(db(peak(control[TAP.echoIn], tm + 0.11, RENDER_END), inRef)).toBeGreaterThan(
			CONTROL_FLOOR_DB
		);
	});

	test('with the cue on and the fader at 0 the cue sum carries the wet signal', async ({
		page
	}) => {
		const bursts = [0.05, 0.3];
		const out = await render(page, { ...NO_CUT, bursts, faderStart: 0, cueOn: true });
		expect(out[TAP.master].every((s) => s === 0)).toBe(true);
		const cue = out[TAP.cueSum];
		expect(db(peak(cue, 0.05, 0.05 + BURST_SEC), 0.5)).toBeGreaterThan(-6);
		const dryEnd = 0.3 + BURST_SEC + 0.05;
		const firstRepeat = peak(cue, 0.3 + ECHO_SEC - 0.01, 0.3 + ECHO_SEC + BURST_SEC + 0.01);
		const [r1, r2] = repeatPeaks(cue, 0.3, dryEnd, 2);
		expect(db(firstRepeat, 0.5)).toBeGreaterThan(-40);
		expect(db(r1, firstRepeat)).toBeGreaterThan(REPEAT_FLOOR_DB);
		expect(db(r2, firstRepeat)).toBeGreaterThan(REPEAT_FLOOR_DB);
		const wet = out[TAP.echoReturn];
		let worst = 0;
		for (let i = frameAt(dryEnd); i < cue.length; i++)
			worst = Math.max(worst, Math.abs(cue[i] - wet[i]));
		expect(worst).toBeLessThanOrEqual(CUE_WET_TOL);
	});
});
