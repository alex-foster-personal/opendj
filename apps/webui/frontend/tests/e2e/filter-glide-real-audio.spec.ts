/**
 * Filter corner glide retargets (FXFILT-10611), rendered by Chromium's own AudioParam.
 *
 * The unit tests (tests/unit/filter-glide.test.mjs) replay the Rust flick
 * fixture through a spec-faithful AudioParam stand-in
 * (tests/unit/fixtures/spec-audio-param.mjs). This spec proves that stand-in:
 * it renders the PRODUCTION glideCorner on a ConstantSourceNode offset through
 * an OfflineAudioContext, retargeting inside ctx.suspend at whole render
 * quanta with ctx.currentTime (as applyColorFxTargets does), and compares every
 * frame with the stand-in rendered between the same retargets.
 *
 * Retargets land every 128 frames because suspend resolves only on quantum
 * boundaries; the Rust fixture keeps its 120-frame cadence and stays the
 * web/Rust evidence.
 *
 * A control subject schedules every octave retarget up front, before rendering.
 * Chromium cancels each curve that is active at the next retarget, so that
 * history differs from the progressive render; a passing control would mean the
 * comparison is blind. Bounds measured Mon 9 Nov 2026 (values beside each).
 *
 * Runs under the root playwright.config.ts; it only visits about:blank.
 *
 * Requirements:
 *
 * - ✔︎ ✅ 🎯 Successive octave retargets through a real AudioParam match the stand-in within 1e-6 per frame.
 * - ✔︎ ✅ 🎯 Successive hz retargets through a real AudioParam match the stand-in within the hz bound.
 * - ✔︎ ✅ 🎯 Retargets that land while the previous curve is active are cancelled as the stand-in models.
 *
 * Acceptance tests:
 *
 * - [if] an octave flick or reversal rendered by Chromium differs from the stand-in by more than 1e-6 relative on any frame [then ⛔️] the stand-in is not spec-faithful.
 * - [if] an hz flick or reversal differs by more than the hz bound [then ⛔️] the stand-in's setTargetAtTime drifts from Chromium.
 * - [if] the schedule-up-front control matches the progressive stand-in within 1e-2 [then ⛔️] the detector is blind.
 */
import { expect, test, type Page } from '@playwright/test';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

import { SpecAudioParam } from '../unit/fixtures/spec-audio-param.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SR = 48_000;
const QUANTUM = 128;
const EVENTS = 24;
const FRAMES = 9600;
/** Octave bound: curve points are float32 and land on whole frames. */
const OCTAVE_MAX_REL = 1e-6;
/**
 * Hz bound: Chromium runs setTargetAtTime in float32, so it drifts from the
 * float64 stand-in (measured Mon 9 Nov 2026: worst 1.31e-4).
 */
const HZ_MAX_REL = 2e-4;
/** Control must differ by more than this (measured Mon 9 Nov 2026: 52.1; octave worst 1.2e-15). */
const CONTROL_MIN_REL = 1e-2;

type Domain = 'hz' | 'octave';
type Corner = 'lpHz' | 'hpHz';
interface Gesture {
	name: string;
	corner: Corner;
	knobs: number[];
}

const ramp = (from: number, to: number, n: number): number[] =>
	Array.from({ length: n }, (_, i) => from + ((to - from) * (i + 1)) / n);

const GESTURES: Gesture[] = [
	{ name: 'lp_flick', corner: 'lpHz', knobs: ramp(0.5, 0, EVENTS) },
	{ name: 'hp_flick', corner: 'hpHz', knobs: ramp(0.5, 1, EVENTS) },
	{
		name: 'lp_reversal',
		corner: 'lpHz',
		knobs: [...ramp(0.5, 0, EVENTS / 2), ...ramp(0, 0.5, EVENTS / 2)]
	}
];

async function glideBundle(format: 'iife' | 'esm'): Promise<string> {
	const result = await build({
		stdin: {
			contents: [
				"export * from './src/lib/rb/filter-glide';",
				"export { FILTER_GLIDE_S } from './src/lib/player/constants';",
				"export { filterParamsFromKnob } from './src/lib/rb/audio-engine-guards';"
			].join('\n'),
			resolveDir: FRONTEND_ROOT,
			loader: 'ts'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: `${FRONTEND_ROOT}/src/lib` },
		bundle: true,
		format,
		...(format === 'iife' ? { globalName: 'filterGlide' } : {}),
		platform: format === 'iife' ? 'browser' : 'node',
		write: false,
		logLevel: 'silent'
	});
	const [output] = result.outputFiles ?? [];
	if (output === undefined) throw new Error('esbuild produced no output file');
	return output.text;
}

/** Render the gesture's schedule on a real AudioParam; `upfront` schedules every retarget before rendering. */
async function renderChromium(
	page: Page,
	opts: { domain: Domain; hzs: number[]; startHz: number; upfront: boolean }
): Promise<number[]> {
	return page.evaluate(
		async ({ domain, hzs, startHz, upfront, sr, quantum, frames }) => {
			// eslint-disable-next-line @typescript-eslint/no-explicit-any
			const mod = (globalThis as any).filterGlide;
			const ctx = new OfflineAudioContext(1, frames, sr);
			const src = new ConstantSourceNode(ctx, { offset: startHz });
			src.connect(ctx.destination);
			src.start(0);
			const glide = (hz: number, now: number) =>
				mod.glideCorner(src.offset, hz, now, sr, domain, mod.FILTER_GLIDE_S);
			glide(hzs[0], 0);
			for (let i = 1; i < hzs.length; i++) {
				const at = (i * quantum) / sr;
				if (upfront) glide(hzs[i], at);
				else
					ctx.suspend(at).then(() => {
						glide(hzs[i], ctx.currentTime);
						ctx.resume();
					});
			}
			const rendered = await ctx.startRendering();
			return Array.from(rendered.getChannelData(0));
		},
		{ ...opts, sr: SR, quantum: QUANTUM, frames: FRAMES }
	);
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function renderStandIn(mod: any, domain: Domain, hzs: number[], startHz: number): number[] {
	const p = new SpecAudioParam(startHz, SR);
	hzs.forEach((hz, i) => {
		p.renderTo(i * QUANTUM);
		mod.glideCorner(p, hz, (i * QUANTUM) / SR, SR, domain, mod.FILTER_GLIDE_S);
	});
	p.renderTo(FRAMES);
	return p.rendered;
}

function worstRelError(got: number[], want: number[]): number {
	expect(got.length).toBe(want.length);
	return got.reduce((worst, g, i) => Math.max(worst, Math.abs(g - want[i]) / want[i]), 0);
}

test('glideCorner retargets through a real AudioParam match the spec-faithful stand-in', async ({
	page
}) => {
	const esm = await glideBundle('esm');
	const mod = await import(`data:text/javascript;base64,${Buffer.from(esm).toString('base64')}`);
	await page.goto('about:blank');
	await page.addScriptTag({ content: await glideBundle('iife') });

	for (const domain of ['hz', 'octave'] as const) {
		let worst = 0;
		for (const g of GESTURES) {
			const startHz = mod.filterParamsFromKnob(0.5)[g.corner];
			const hzs = g.knobs.map((k) => mod.filterParamsFromKnob(k)[g.corner]);
			const got = await renderChromium(page, { domain, hzs, startHz, upfront: false });
			const err = worstRelError(got, renderStandIn(mod, domain, hzs, startHz));
			console.log(`${domain} worst ${g.name}: ${err}`);
			worst = Math.max(worst, err);
		}
		expect(worst, `${domain} worst relative error`).toBeLessThanOrEqual(
			domain === 'octave' ? OCTAVE_MAX_REL : HZ_MAX_REL
		);
	}

	const reversal = GESTURES[2];
	const startHz = mod.filterParamsFromKnob(0.5)[reversal.corner];
	const hzs = reversal.knobs.map((k) => mod.filterParamsFromKnob(k)[reversal.corner]);
	const control = await renderChromium(page, { domain: 'octave', hzs, startHz, upfront: true });
	const controlErr = worstRelError(control, renderStandIn(mod, 'octave', hzs, startHz));
	console.log(`control worst: ${controlErr}`);
	expect(controlErr, 'schedule-up-front control must differ').toBeGreaterThan(CONTROL_MIN_REL);
});
