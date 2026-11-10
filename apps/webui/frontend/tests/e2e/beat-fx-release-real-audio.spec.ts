/**
 * Beat FX quantize and Release FX echo (BEATFX-10766, BEATFX-10766b), rendered by a real Web Audio graph.
 *
 * Renders the PRODUCTION `buildBeatFxRelease`, `planQuantizedTransition`, `scheduleBeatFxEnable` and
 * `buildEchoKernel` through Chromium's OfflineAudioContext at 48 kHz. The unit test
 * (tests/unit/beat-fx-quantize.test.mjs) is the supplementary per-sample simulator.
 * Release wet is derived as out minus dry probe minus beat FX wet probe, so a broken
 * `tap -> releaseSend` or `releaseReturn -> out` wiring fails here.
 *
 * Measured values beside each threshold (Mon 9 Nov 2026): boundary offset_ms<=0.017;
 * click ON/OFF 0.174/0.874 (beats=1/4), 0.879/0.879 (beats=1);
 * release onset 0 ms; dry restore 0 ms; derive residual<=1e-6; ring-out tail peak 9.48e-5;
 * release click press/release 0.924/0.916 (nucbox-wsl Chromium).
 *
 * Runs under playwright.config.ts; it only visits about:blank.
 *
 * Requirements:
 *
 * - ✔︎ ✅ 🎯 Quantized ON and OFF land within 2 ms of the next min(division, 1 beat) boundary, never a beat late.
 * - ✔︎ ✅ 🎯 Quantized transitions and Release FX press and release score click ratio at or under 1.5.
 * - ✔︎ ✅ 🎯 Release press cuts the dry, the beat FX tail rings, and the release echo sounds on the beat.
 * - ✔︎ ✅ 🎯 Release restores the dry on the next boundary and the release echo rings out below -60 dBFS in 5 s.
 *
 * Acceptance tests:
 *
 * - [if] quantized ON or OFF departs over 2 ms from the boundary or over a beat late [then ⛔️] engage is off-grid.
 * - [if] release press leaves dry or beat FX tail wrong, or release echo onset is off [then ⛔️] hold is broken.
 * - [if] release does not restore dry on the boundary or echo does not decay [then ⛔️] ring-out is broken.
 * - [if] any transition or Release FX move scores click ratio over 1.5 [then ⛔️] the ramp clicks.
 */
// requirement: BEATFX-10766
// requirement: BEATFX-10766b
import { expect, test, type Page } from '@playwright/test';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

import {
	clickRatio,
	SLIP_RETURN_CLICK_VETO,
	assertBlendWindowClean
} from '../../src/lib/rb/slip-return-blend';
import { nextQuantizeBoundarySec } from '../../src/lib/rb/beat-fx';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SR = 48_000;
const QUANTUM = 128;
const BPM = 124;
const BEAT_SEC = 60 / BPM;
const GRID0 = 0.1;
const GRID = Array.from({ length: 32 }, (_, k) => GRID0 + k * BEAT_SEC);
const TWO_MS = Math.round(0.002 * SR);
const DERIVE_FLOOR = 1e-6;
const ONSET = 1e-5;

type Bundle = typeof import('../../src/lib/rb/beat-fx-quantize') &
	Pick<typeof import('../../src/lib/rb/beat-fx'), 'nextQuantizeBoundarySec'> &
	typeof import('../../src/lib/rb/fx/echo-kernel');

type Move =
	| { kind: 'enable'; at: number; on: boolean; beats: number }
	| { kind: 'press'; at: number; beats: number }
	| { kind: 'release'; at: number; beats: number };

type RenderPlan = {
	signal: 'music' | 'dc';
	seconds: number;
	initial: { send: number; dry: number };
	moves: Move[];
	/** Default records all four probe channels; boundary runs only need beat FX wet and send gain. */
	recordChannels?: number[];
};

type RenderResult = {
	channels: number[][];
	plans: Array<{ kind: string; atSec: number; state?: string }>;
	rampSec: number;
};

const q = (sec: number): number => (Math.round((sec * SR) / QUANTUM) * QUANTUM) / SR;

async function releaseBundle(): Promise<string> {
	const result = await build({
		stdin: {
			contents: [
				"export * from './src/lib/rb/beat-fx-quantize';",
				"export { nextQuantizeBoundarySec } from './src/lib/rb/beat-fx';",
				"export { buildEchoKernel } from './src/lib/rb/fx/echo-kernel';"
			].join('\n'),
			resolveDir: FRONTEND_ROOT,
			loader: 'ts'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: `${FRONTEND_ROOT}/src/lib` },
		bundle: true,
		format: 'iife',
		globalName: 'beatFxRelease',
		platform: 'browser',
		write: false,
		logLevel: 'silent'
	});
	return result.outputFiles[0].text;
}

async function render(page: Page, plan: RenderPlan): Promise<RenderResult> {
	return page.evaluate(
		async ({ plan, sr, quantum, bpm, beatSec, grid0, grid, recordChannels }) => {
			const mod = (globalThis as unknown as { beatFxRelease: Bundle }).beatFxRelease;
			const qLocal = (sec: number) => (Math.round((sec * sr) / quantum) * quantum) / sr;
			const frames = Math.round(plan.seconds * sr);
			const ctx = new OfflineAudioContext(4, frames, sr);

			let source: AudioNode;
			if (plan.signal === 'dc') {
				const dc = ctx.createConstantSource();
				dc.offset.value = 0.5;
				dc.start(0);
				source = dc;
			} else {
				const buffer = ctx.createBuffer(1, frames, sr);
				const x = buffer.getChannelData(0);
				for (let n = 0; n < frames; n++) x[n] = 0.2 * Math.sin((2 * Math.PI * 220 * n) / sr);
				const burst = Math.round(0.08 * sr);
				for (const b of grid) {
					const s = Math.round(b * sr);
					for (let i = 0; i < burst && s + i < frames; i++)
						x[s + i] += 0.2 * Math.exp(-i / (0.03 * sr)) * Math.sin((2 * Math.PI * 1000 * i) / sr);
				}
				const player = ctx.createBufferSource();
				player.buffer = buffer;
				player.start(0);
				source = player;
			}

			const tap = ctx.createGain();
			tap.gain.value = 1;
			source.connect(tap);

			const dry = ctx.createGain();
			dry.gain.value = plan.initial.dry;
			const beatFxSend = ctx.createGain();
			beatFxSend.gain.value = plan.initial.send;
			const out = ctx.createGain();

			tap.connect(dry);
			dry.connect(out);

			const beatFx = mod.buildEchoKernel(ctx, {
				maxDelaySec: 2,
				feedback: mod.BEAT_FX_RELEASE_FEEDBACK,
				loopHpHz: mod.BEAT_FX_RELEASE_LOOP_HP_HZ,
				loopLpHz: mod.BEAT_FX_RELEASE_LOOP_LP_HZ
			});
			beatFx.setTime(beatSec / 2, 0);
			tap.connect(beatFxSend);
			beatFxSend.connect(beatFx.input);
			beatFx.output.connect(out);

			const release = mod.buildBeatFxRelease(ctx, { dry, beatFxSend, tap, out });

			const merger = ctx.createChannelMerger(4);
			merger.connect(ctx.destination);
			out.connect(merger, 0, 0);
			dry.connect(merger, 0, 1);
			beatFx.output.connect(merger, 0, 2);
			beatFxSend.connect(merger, 0, 3);

			const returnedPlans: Array<{ kind: string; atSec: number; state?: string }> = [];

			for (const move of plan.moves) {
				const at = qLocal(move.at);
				ctx.suspend(at).then(() => {
					const nowSec = ctx.currentTime;
					if (move.kind === 'enable') {
						const quantPlan = mod.planQuantizedTransition({
							quantize: true,
							gridBeatsSec: grid,
							nowSec,
							beats: move.beats
						});
						mod.scheduleBeatFxEnable(beatFxSend, move.on, quantPlan);
						returnedPlans.push({
							kind: 'enable',
							atSec: quantPlan.atSec,
							state: quantPlan.state
						});
					} else if (move.kind === 'press') {
						release.press({
							beats: move.beats,
							bpm,
							plan: { state: 'off', atSec: nowSec }
						});
						returnedPlans.push({ kind: 'press', atSec: nowSec });
					} else if (move.kind === 'release') {
						const quantPlan = mod.planQuantizedTransition({
							quantize: true,
							gridBeatsSec: grid,
							nowSec,
							beats: move.beats
						});
						release.release(quantPlan);
						returnedPlans.push({
							kind: 'release',
							atSec: quantPlan.atSec,
							state: quantPlan.state
						});
					}
					ctx.resume();
				});
			}

			const rendered = await ctx.startRendering();
			const chans = recordChannels ?? [0, 1, 2, 3];
			return {
				channels: chans.map((c) => Array.from(rendered.getChannelData(c))),
				plans: returnedPlans,
				rampSec: mod.BEAT_FX_RAMP_SEC
			};
		},
		{
			plan,
			sr: SR,
			quantum: QUANTUM,
			bpm: BPM,
			beatSec: BEAT_SEC,
			grid0: GRID0,
			grid: GRID,
			recordChannels: plan.recordChannels
		}
	);
}

function deriveRelWet(out: number[], dry: number[], beatWet: number[]): Float32Array {
	const rel = new Float32Array(out.length);
	for (let n = 0; n < out.length; n++) rel[n] = out[n] - dry[n] - beatWet[n];
	return rel;
}

const firstDeparture = (curve: number[], from: number): number => {
	for (let n = from + 1; n < curve.length; n++) if (curve[n] !== curve[from]) return n;
	return -1;
};

const peakOf = (a: number[] | Float32Array, from: number, to: number): number => {
	let p = 0;
	for (let n = from; n < to; n++) p = Math.max(p, Math.abs(a[n]));
	return p;
};

test.describe('beat FX quantize and Release FX (real audio)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('about:blank');
		await page.addScriptTag({ content: await releaseBundle() });
	});

	test('quantized ON and OFF land on the boundary within 2 ms', async ({ page }) => {
		const cases: Array<{ beats: number; nowSec: number }> = [];
		for (const beats of [1 / 16, 1, 4])
			cases.push({ beats, nowSec: GRID0 + 0.37 * BEAT_SEC });

		for (const { beats, nowSec } of cases) {
			const nowQ = q(nowSec);
			const { plans } = await render(page, {
				signal: 'dc',
				seconds: 2.5,
				initial: { send: 0, dry: 1 },
				moves: [{ kind: 'enable', at: nowQ, on: true, beats }],
				recordChannels: [2, 3]
			});
			const onPlan = plans.find((p) => p.kind === 'enable');
			expect(onPlan).toBeDefined();
			expect(onPlan!.state).toBe('on');
			expect(onPlan!.atSec - nowQ).toBeLessThanOrEqual(BEAT_SEC + 1e-9);

			const offNow = q(onPlan!.atSec + 0.61 * BEAT_SEC);
			const offResult = await render(page, {
				signal: 'dc',
				seconds: 2.5,
				initial: { send: 0, dry: 1 },
				moves: [
					{ kind: 'enable', at: nowQ, on: true, beats },
					{ kind: 'enable', at: offNow, on: false, beats }
				],
				recordChannels: [2, 3]
			});
			const beatWet = offResult.channels[0];
			const sendProbe = offResult.channels[1];
			const offPlan = offResult.plans.filter((p) => p.kind === 'enable')[1];

			for (const [plan, now, label] of [
				[onPlan!, nowQ, 'ON'],
				[offPlan, offNow, 'OFF']
			] as const) {
				const expected = nextQuantizeBoundarySec(GRID, now, beats)! * SR;
				const dep = firstDeparture(sendProbe, Math.floor(now * SR));
				const offsetMs = Math.abs(dep - expected) / (SR / 1000);
				console.log(`${label} beats=${beats} offset_ms=${offsetMs.toFixed(3)}`);
				expect(Math.abs(dep - expected)).toBeLessThanOrEqual(TWO_MS);
				expect(plan.atSec - now).toBeLessThanOrEqual(BEAT_SEC + 1e-9);
			}

			const onDep = firstDeparture(sendProbe, Math.floor(nowQ * SR));
			expect(peakOf(beatWet, 0, onDep)).toBeLessThanOrEqual(DERIVE_FLOOR);
			expect(peakOf(beatWet, onDep + TWO_MS, beatWet.length)).toBeGreaterThan(ONSET);
		}
	});

	test('quantized ON and OFF click ratio at or under 1.5', async ({ page }) => {
		for (const beats of [1 / 4, 1]) {
			const nowSec = q(GRID0 + 0.37 * BEAT_SEC);
			const held0 = await render(page, {
				signal: 'music',
				seconds: 4,
				initial: { send: 0, dry: 1 },
				moves: []
			});
			const held1 = await render(page, {
				signal: 'music',
				seconds: 4,
				initial: { send: 1, dry: 1 },
				moves: []
			});
			const offNow = q(nowSec + 0.61 * BEAT_SEC);
			const switched = await render(page, {
				signal: 'music',
				seconds: 4,
				initial: { send: 0, dry: 1 },
				moves: [
					{ kind: 'enable', at: nowSec, on: true, beats },
					{ kind: 'enable', at: offNow, on: false, beats }
				]
			});
			const out = new Float32Array(switched.channels[0]);
			const h0 = new Float32Array(held0.channels[0]);
			const h1 = new Float32Array(held1.channels[0]);
			assertBlendWindowClean(out, 0, out.length);
			const [onPlan, offPlan] = switched.plans.filter((p) => p.kind === 'enable');
			const onCr = clickRatio(out, [h0, h1], Math.round(onPlan.atSec * SR));
			const offCr = clickRatio(out, [h0, h1], Math.round(offPlan.atSec * SR));
			console.log(`click beats=${beats}: ON ${onCr.toFixed(3)}, OFF ${offCr.toFixed(3)}`);
			expect(onCr).toBeLessThanOrEqual(SLIP_RETURN_CLICK_VETO);
			expect(offCr).toBeLessThanOrEqual(SLIP_RETURN_CLICK_VETO);
		}
	});

	test('release press cuts dry, beat FX tail rings, echo on the beat', async ({ page }) => {
		const beats = 1 / 2;
		const t0 = q(GRID0 + 4.2 * BEAT_SEC);
		const { channels, rampSec } = await render(page, {
			signal: 'music',
			seconds: t0 + 1.2,
			initial: { send: 1, dry: 1 },
			moves: [{ kind: 'press', at: t0, beats }]
		});
		const out = channels[0];
		const dryProbe = channels[1];
		const beatWet = channels[2];
		const relWet = deriveRelWet(out, dryProbe, beatWet);

		const after = Math.ceil((t0 + rampSec + 0.001) * SR);
		const to = Math.round((t0 + beats * BEAT_SEC) * SR);
		expect(peakOf(dryProbe, after, to)).toBe(0);
		expect(peakOf(beatWet, Math.round((t0 + 0.02) * SR), to)).toBeGreaterThan(0.01);
		expect(peakOf(relWet, 0, Math.floor(t0 * SR))).toBeLessThanOrEqual(DERIVE_FLOOR);

		let first = -1;
		for (let n = Math.floor(t0 * SR); n < relWet.length; n++) {
			if (Math.abs(relWet[n]) > ONSET) {
				first = n;
				break;
			}
		}
		const expected = Math.round((t0 + beats * BEAT_SEC) * SR);
		console.log(`release onset: first=${first} expected=${expected}`);
		expect(Math.abs(first - expected)).toBeLessThanOrEqual(TWO_MS);
		const heldEnd = Math.round((t0 + 1) * SR);
		expect(peakOf(relWet, to, heldEnd)).toBeGreaterThan(0.01);
	});

	test('release restores dry on boundary and echo rings out', async ({ page }) => {
		const beats = 1 / 2;
		const t0 = q(GRID0 + 4.2 * BEAT_SEC);
		const t1 = q(GRID0 + 6.3 * BEAT_SEC);
		const expected = nextQuantizeBoundarySec(GRID, t1, beats)!;

		const dcResult = await render(page, {
			signal: 'dc',
			seconds: expected + 0.5,
			initial: { send: 1, dry: 1 },
			moves: [
				{ kind: 'press', at: t0, beats },
				{ kind: 'release', at: t1, beats }
			]
		});
		const releasePlan = dcResult.plans.find((p) => p.kind === 'release');
		expect(releasePlan!.atSec).toBeCloseTo(expected, 9);
		const dryProbe = dcResult.channels[1];
		const dep = firstDeparture(dryProbe, Math.floor(t1 * SR));
		console.log(`dry restore dep=${dep} expected=${Math.round(expected * SR)}`);
		expect(Math.abs(dep - expected * SR)).toBeLessThanOrEqual(TWO_MS);

		const musicSeconds = expected + dcResult.rampSec + 5.5;
		const music = await render(page, {
			signal: 'music',
			seconds: musicSeconds,
			initial: { send: 1, dry: 1 },
			moves: [
				{ kind: 'press', at: t0, beats },
				{ kind: 'release', at: t1, beats }
			]
		});
		const relWet = deriveRelWet(music.channels[0], music.channels[1], music.channels[2]);
		const end = Math.round((expected + music.rampSec) * SR);
		const period = Math.round(beats * BEAT_SEC * SR);
		expect(peakOf(relWet, end, end + period)).toBeGreaterThan(0.01);
		const tail = end + 5 * SR;
		const tailPeak = peakOf(relWet, tail, relWet.length);
		console.log(`ring-out tail peak=${tailPeak}`);
		expect(tailPeak).toBeLessThan(1e-3);
	});

	test('release press and release click ratio at or under 1.5', async ({ page }) => {
		const beats = 1 / 2;
		const t0 = q(GRID0 + 4.2 * BEAT_SEC);
		const t1 = q(GRID0 + 6.3 * BEAT_SEC);
		const seconds = t1 + 5.5;

		const before = await render(page, {
			signal: 'music',
			seconds,
			initial: { send: 1, dry: 1 },
			moves: []
		});
		const heldOnly = await render(page, {
			signal: 'music',
			seconds,
			initial: { send: 1, dry: 1 },
			moves: [{ kind: 'press', at: t0, beats }]
		});
		const full = await render(page, {
			signal: 'music',
			seconds,
			initial: { send: 1, dry: 1 },
			moves: [
				{ kind: 'press', at: t0, beats },
				{ kind: 'release', at: t1, beats }
			]
		});
		const out = new Float32Array(full.channels[0]);
		const bOut = new Float32Array(before.channels[0]);
		const hOut = new Float32Array(heldOnly.channels[0]);
		const relWet = deriveRelWet(full.channels[0], full.channels[1], full.channels[2]);
		const heldStart = Math.round((t0 + beats * BEAT_SEC) * SR);
		const heldEnd = Math.round((t0 + 0.9) * SR);
		expect(peakOf(relWet, heldStart, heldEnd)).toBeGreaterThan(0.01);
		const releasePlan = full.plans.find((p) => p.kind === 'release')!;
		const pressCr = clickRatio(out, [bOut, hOut], Math.round(t0 * SR));
		const releaseCr = clickRatio(out, [hOut, bOut], Math.round(releasePlan.atSec * SR));
		console.log(`release click: press ${pressCr.toFixed(3)}, release ${releaseCr.toFixed(3)}`);
		expect(pressCr).toBeLessThanOrEqual(SLIP_RETURN_CLICK_VETO);
		expect(releaseCr).toBeLessThanOrEqual(SLIP_RETURN_CLICK_VETO);
	});
});
