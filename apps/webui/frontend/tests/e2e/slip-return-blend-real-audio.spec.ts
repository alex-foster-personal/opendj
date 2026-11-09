/**
 * Slip return crossfade (FXROLL-10595, issue #9842), rendered through real Web Audio graphs.
 *
 * The unit tests (tests/unit/slip-return-blend.test.mjs) check the gains, the anchor
 * algebra and the automation arm() schedules on a fake context. This spec decodes the
 * tracked MP3 fixtures in Chromium (decodeAudioData, the deck's decoder) and releases a
 * roll with the PRODUCTION prepareSlipReturnBlend().arm() in an OfflineAudioContext,
 * through two real graphs, and measures the rendered samples:
 *
 * - `worklet`: the deck graph. The pinned Signalsmith stretch worklet, created with the
 *   deck's STRETCH_NODE_OPTIONS, plays the roll as a worklet loop into an analyser.
 *   Release: from ctx.suspend at least processorOnsetLeadSec(latency) ahead of T,
 *   arm(T) then node.schedule(T, hidden head, loop cleared), the order _resumeSlip uses.
 * - `raw`: the same release with the processor replaced by AudioBufferSourceNodes of the
 *   decoded audio (looped source stopped at T, hidden-head source started at T). It has
 *   no STFT smoothing and no phase state, so it is where a hard cut really clicks and
 *   where a null against the never-rolled render resolves the hidden head to 1 ms.
 *
 * What the worklet render showed (Mon 9 Nov 2026): a hard seek through Signalsmith does
 * not click (worst ratio 1.00): every block re-reads its input window and overlap-adds,
 * which already smooths the jump. Its phase vocoder keeps phase history, so after any
 * jump on tonal material the output never nulls against a never-rolled render (hard cut
 * and blend alike, -12 to -37 dBFS), while a seek with no jump nulls exactly. So on the
 * worklet graph the null is taken against the hard release: after the window the blend
 * must leave nothing behind. The hidden head and the click veto that picks the length
 * are proved on the raw graph, and the worklet graph must still pass the veto.
 *
 * Timeline (48 kHz, whole 128-frame quanta): the roll engages at output t = 0 on a beat
 * about 4 s into the track, loop [engage, engage + roll). Release at T = 2.1 s, off the
 * beat grid at every fixture tempo; the
 * hidden head at T is engage + T (rate 1).
 *
 * Fixtures: 06 click-90 (90 BPM), 04 kick-124 (124 BPM), 05 a-minor and 03 voice (no
 * grid in the repo, nominal 120 BPM; a roll is only a frame count, so the nominal tempo
 * only sets the roll lengths). Rolls 1/4, 1 and 4 beats. A fresh page per test keeps
 * each page under Chromium's about-30-offline-renders wedge. Serial, because the last
 * test reads the per-length worst ratios the raw tests record.
 *
 * Runs under the root playwright.config.ts on a synthetic origin; no port of its own.
 *
 * Requirements:
 *
 * - ✔︎ ✅ 🎯 The release click ratio is at or under SLIP_RETURN_CLICK_VETO at the chosen length, both graphs.
 * - ✔︎ ✅ 🎯 SLIP_RETURN_BLEND_FRAMES is the shortest candidate that passes on every fixture x roll.
 * - ✔︎ ✅ 🎯 After the window the raw release nulls against the never-rolled render (hidden head exact).
 * - ✔︎ ✅ 🎯 After the window the worklet release nulls against the worklet hard release.
 * - ✔︎ ✅ 🎯 The crossfade window has no sample over 0 dBFS, no NaN, no subnormal.
 * - ✔︎ ✅ 🎯 The worklet render is deterministic.
 *
 * Acceptance tests:
 *
 * - [if] a blend release has a click ratio above 1.5 on any fixture x roll x graph [then ⛔️] the release clicks.
 * - [if] a shorter candidate passes, or the chosen one fails [then ⛔️] the length is not the measured shortest.
 * - [if] the raw blend minus the never-rolled render is above -60 dBFS RMS over the second after the window [then ⛔️] position_ms is off the hidden head.
 * - [if] the worklet blend minus the worklet hard release is above -60 dBFS RMS after the window [then ⛔️] the old tail leaks past the window.
 * - [if] the window has a sample over 0 dBFS, a NaN or a subnormal [then ⛔️] the blend is unclean.
 * - [if] the raw hard cut passes the veto on the click fixture, or a 1 ms head offset passes the null [then ⛔️] the detector is blind.
 * - [if] two identical worklet renders differ [then ⛔️] the instrument is not repeatable.
 */
import { expect, test, type Page, type Route } from '@playwright/test';
import { build } from 'esbuild';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
	assertBlendWindowClean,
	clickRatio,
	SLIP_RETURN_BLEND_CANDIDATES,
	SLIP_RETURN_BLEND_FRAMES,
	SLIP_RETURN_CLICK_VETO
} from '../../src/lib/rb/slip-return-blend';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const FIXTURE_DIR = join(FRONTEND_ROOT, '../../../tests/fixtures/app-acceptance/library');
const ORIGIN = 'https://slip-return-blend.invalid';
const SR = 48_000;
const QUANTUM = 128;
const RENDER_SEC = 3.5;
/** Off the beat grid at every fixture tempo (3.15 beats at 90, 4.2 at 120, 4.34 at 124):
 * a release can land anywhere, and one on a beat joins two onsets and hides the cut. */
const RELEASE_FRAME = Math.round((2.1 * SR) / QUANTUM) * QUANTUM;
const NULL_DBFS_MAX = -60;
const ROLL_BEATS = [0.25, 1, 4];
const FIXTURES = [
	{ file: '06-acceptance-click-90.mp3', bpm: 90 },
	{ file: '04-acceptance-kick-124.mp3', bpm: 124 },
	{ file: '05-acceptance-a-minor.mp3', bpm: 120 },
	{ file: '03-acceptance-voice.mp3', bpm: 120 }
];

type Graph = 'worklet' | 'raw';
/** blend: production arm, with the processor flag _resumeSlip passes for the graph;
 * forced: arm told the processor does not crossfade its seeks (worklet control);
 * hard: same release, no arm; looped: never released; noroll: never rolled;
 * offset: blend released 1 ms past the hidden head. */
type Subject = 'blend' | 'forced' | 'hard' | 'looped' | 'noroll' | 'offset';
type SlipBundle = typeof import('../../src/lib/rb/slip-return-blend') &
	Pick<
		typeof import('../../src/lib/rb/stretch-adapter'),
		'STRETCH_NODE_OPTIONS' | 'buildStretchSchedule'
	> &
	Pick<
		typeof import('../../src/lib/player/transport/schedule-math'),
		'stretchScheduleChange' | 'processorOnsetLeadSec'
	>;

interface RenderOpts {
	graph: Graph;
	file: string;
	bpm: number;
	rollBeats: number;
	subject: Subject;
	frames: number;
}

type Channels = number[][];

/** Worst raw click ratio per crossfade length (and 'hard'), over every fixture x roll. */
const RAW_WORST = new Map<number | 'hard', number>();
/** Worst worklet click ratio of the forced blend (the control for the skip). */
const WORKLET_WORST = new Map<'forced', number>();

async function slipBundle(): Promise<string> {
	const result = await build({
		stdin: {
			contents: [
				"export * from './src/lib/rb/slip-return-blend';",
				"export { STRETCH_NODE_OPTIONS, buildStretchSchedule } from './src/lib/rb/stretch-adapter';",
				"export { stretchScheduleChange, processorOnsetLeadSec } from './src/lib/player/transport/schedule-math';"
			].join('\n'),
			resolveDir: FRONTEND_ROOT,
			loader: 'ts'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: `${FRONTEND_ROOT}/src/lib` },
		bundle: true,
		format: 'iife',
		globalName: 'slipBlend',
		platform: 'browser',
		write: false,
		logLevel: 'silent'
	});
	return result.outputFiles[0].text;
}

async function openPage(page: Page): Promise<void> {
	const moduleSource = readFileSync(
		join(FRONTEND_ROOT, 'node_modules/signalsmith-stretch/SignalsmithStretch.mjs'),
		'utf8'
	);
	await page.route(`${ORIGIN}/**`, (route: Route) => {
		const path = new URL(route.request().url()).pathname;
		if (path === '/') {
			return route.fulfill({
				status: 200,
				contentType: 'text/html',
				body: '<!doctype html><body></body>'
			});
		}
		if (path === '/SignalsmithStretch.mjs') {
			return route.fulfill({ status: 200, contentType: 'text/javascript', body: moduleSource });
		}
		const fixture = /^\/fixtures\/([\w.-]+\.mp3)$/.exec(path);
		if (fixture) {
			return route.fulfill({
				status: 200,
				contentType: 'audio/mpeg',
				body: readFileSync(join(FIXTURE_DIR, fixture[1]))
			});
		}
		return route.fulfill({ status: 404, body: 'not served' });
	});
	await page.goto(`${ORIGIN}/`);
	await page.addScriptTag({ content: await slipBundle() });
}

async function render(page: Page, opts: RenderOpts): Promise<Channels> {
	return page.evaluate(
		async ({
			graph,
			file,
			bpm,
			rollBeats,
			subject,
			frames,
			sr,
			quantum,
			seconds,
			releaseFrame
		}) => {
			const mod = (globalThis as unknown as { slipBlend: SlipBundle }).slipBlend;
			const scope = globalThis as unknown as Record<string, unknown>;
			if (scope.__decodedFile !== file) {
				const bytes = await (await fetch(`${location.origin}/fixtures/${file}`)).arrayBuffer();
				scope.__decoded = await new OfflineAudioContext(2, 1, sr).decodeAudioData(bytes);
				scope.__decodedFile = file;
			}
			const decoded = scope.__decoded as AudioBuffer;
			const beat = 60 / bpm;
			// Loop points on whole frames: a fractional buffer-source loop drifts by a
			// fraction of a frame per wrap, which would misplace the raw graph's old tail.
			const engageSec = Math.round(Math.round(4 / beat) * beat * sr) / sr;
			const rollSec = Math.round(rollBeats * beat * sr) / sr;
			const T = releaseFrame / sr;
			const headSec = engageSec + T + (subject === 'offset' ? 0.001 : 0);
			const rolls = subject !== 'noroll';
			const releases = subject !== 'looped' && subject !== 'noroll';
			// _resumeSlip passes `rt.processor instanceof StretchDeckProcessor`.
			const crossfades = graph === 'worklet' && subject !== 'forced';

			const ctx = new OfflineAudioContext(2, Math.round(seconds * sr), sr);
			const analyser = ctx.createAnalyser();
			analyser.connect(ctx.destination);
			let processor: { connect(n: AudioNode): void; disconnect(): void };
			let leadSec: number;
			let release: () => Promise<void>;

			if (graph === 'worklet') {
				const create = (await import(`${location.origin}/SignalsmithStretch.mjs`)).default;
				const node = await create(ctx, mod.STRETCH_NODE_OPTIONS);
				leadSec = mod.processorOnsetLeadSec(await node.latency());
				const pcm = [0, 1].map((c) =>
					decoded.getChannelData(Math.min(c, decoded.numberOfChannels - 1)).slice()
				);
				await node.addBuffers(
					pcm,
					pcm.map((ch) => ch.buffer)
				);
				node.connect(analyser);
				processor = { connect: (n) => node.connect(n), disconnect: () => node.disconnect() };
				const rollLoop = {
					in_ms: engageSec * 1000,
					out_ms: (engageSec + rollSec) * 1000,
					engaged: true,
					beat_length: null
				};
				await node.schedule(
					mod.buildStretchSchedule(
						0,
						mod.stretchScheduleChange(engageSec, true, 1, true, 0, rolls ? rollLoop : null)
					)
				);
				release = async () => {
					await node.schedule(
						mod.buildStretchSchedule(T, mod.stretchScheduleChange(headSec, true, 1, true, 0, null))
					);
				};
			} else {
				const bus = ctx.createGain();
				bus.connect(analyser);
				processor = { connect: (n) => bus.connect(n), disconnect: () => bus.disconnect() };
				leadSec = (2 * quantum) / sr;
				const playing = ctx.createBufferSource();
				playing.buffer = decoded;
				if (rolls) {
					playing.loop = true;
					playing.loopStart = engageSec;
					playing.loopEnd = engageSec + rollSec;
				}
				playing.connect(bus);
				playing.start(0, engageSec);
				release = async () => {
					playing.stop(T);
					const head = ctx.createBufferSource();
					head.buffer = decoded;
					head.connect(bus);
					head.start(T, headSec);
				};
			}

			let releaseError: unknown = null;
			if (releases) {
				const suspendFrame = Math.floor(((T - leadSec) * sr) / quantum) * quantum - quantum;
				ctx.suspend(suspendFrame / sr).then(async () => {
					try {
						if (ctx.currentTime >= T) throw new Error(`release issued late at ${ctx.currentTime}`);
						if (subject !== 'hard') {
							const outcome = mod
								.prepareSlipReturnBlend(
									{
										context: ctx as unknown as AudioContext,
										runtime: { audioBuffer: decoded, nodes: { analyser }, processor, loadToken: 1 },
										isStemProcessor: false,
										processorCrossfadesSeeks: crossfades,
										oldPositionSecAt: (t: number) => engageSec + (t % rollSec),
										loop: { startSec: engageSec, endSec: engageSec + rollSec },
										tempoRatio: 1
									},
									frames
								)
								.arm(T);
							const expected = crossfades ? 'hard:processor-crossfades' : 'blended';
							if (outcome !== expected)
								throw new Error(`arm returned ${outcome}, expected ${expected}`);
						}
						await release();
					} catch (error) {
						releaseError = error;
					}
					await ctx.resume();
				});
			}
			const rendered = await ctx.startRendering();
			// A throw inside the suspend callback must fail the render, not wedge it.
			if (releaseError !== null) throw releaseError;
			return [0, 1].map((c) => Array.from(rendered.getChannelData(c)));
		},
		{ ...opts, sr: SR, quantum: QUANTUM, seconds: RENDER_SEC, releaseFrame: RELEASE_FRAME }
	);
}

//----- metrics

const f32 = (x: number[]): Float32Array => Float32Array.from(x);
const round2 = (x: number): number => Number(x.toFixed(2));

/** Worst channel's RMS of a - b over the second from `start`, in dBFS. */
function nullDbfs(a: Channels, b: Channels, start: number): number {
	return Math.max(
		...[0, 1].map((c) => {
			let sum = 0;
			for (let n = start; n < start + SR; n++) sum += (a[c][n] - b[c][n]) ** 2;
			const rms = Math.sqrt(sum / SR);
			return rms === 0 ? -Infinity : 20 * Math.log10(rms);
		})
	);
}

function worstClickRatio(subject: Channels, looped: Channels, noroll: Channels): number {
	return Math.max(
		...[0, 1].map((c) =>
			clickRatio(f32(subject[c]), [f32(looped[c]), f32(noroll[c])], RELEASE_FRAME)
		)
	);
}

function assertWindowClean(render: Channels, frames: number): void {
	for (const channel of render) {
		assertBlendWindowClean(f32(channel), RELEASE_FRAME - 16, RELEASE_FRAME + frames + 16);
	}
}

function recordRawWorst(key: number | 'hard', ratio: number): void {
	RAW_WORST.set(key, Math.max(RAW_WORST.get(key) ?? 0, ratio));
}

//----- tests

test.describe('slip return crossfade through real Web Audio graphs (FXROLL-10595)', () => {
	test.describe.configure({ mode: 'serial' });
	const frames = SLIP_RETURN_BLEND_FRAMES;

	for (const fixture of FIXTURES) {
		test(`raw graph, ${fixture.file}: click veto per length, hidden-head null`, async ({
			page
		}) => {
			test.setTimeout(180_000);
			await openPage(page);
			const base = { graph: 'raw' as const, file: fixture.file, bpm: fixture.bpm, frames };
			const noroll = await render(page, { ...base, rollBeats: 1, subject: 'noroll' });
			const rows: Record<string, number>[] = [];
			for (const rollBeats of ROLL_BEATS) {
				const looped = await render(page, { ...base, rollBeats, subject: 'looped' });
				const hard = await render(page, { ...base, rollBeats, subject: 'hard' });
				const row: Record<string, number> = {
					roll: rollBeats,
					hard: worstClickRatio(hard, looped, noroll)
				};
				recordRawWorst('hard', row.hard);
				for (const candidate of SLIP_RETURN_BLEND_CANDIDATES) {
					const blend = await render(page, {
						...base,
						rollBeats,
						frames: candidate,
						subject: 'blend'
					});
					row[String(candidate)] = worstClickRatio(blend, looped, noroll);
					recordRawWorst(candidate, row[String(candidate)]);
					if (candidate === frames) {
						row.nullDbfs = nullDbfs(blend, noroll, RELEASE_FRAME + frames);
						assertWindowClean(blend, frames);
					}
				}
				rows.push(row);
			}
			console.log(`raw ${fixture.file}`);
			console.table(
				rows.map((row) => Object.fromEntries(Object.entries(row).map(([k, v]) => [k, round2(v)])))
			);
			for (const row of rows) {
				expect(
					row[String(frames)],
					`if slip return click ratio > 1.5 then broken (roll ${row.roll})`
				).toBeLessThanOrEqual(SLIP_RETURN_CLICK_VETO);
				expect(
					row.nullDbfs,
					`if the release does not null at the hidden head then broken (roll ${row.roll})`
				).toBeLessThanOrEqual(NULL_DBFS_MAX);
			}
			const offset = await render(page, { ...base, rollBeats: 1, subject: 'offset' });
			const offsetDb = nullDbfs(offset, noroll, RELEASE_FRAME + frames);
			console.log(`raw ${fixture.file}: 1 ms offset null ${offsetDb.toFixed(1)} dBFS`);
			expect(offsetDb, 'if a 1 ms head offset nulls then the detector is blind').toBeGreaterThan(
				NULL_DBFS_MAX
			);
		});

		test(`worklet graph, ${fixture.file}: click veto, no leak after the window, determinism`, async ({
			page
		}) => {
			test.setTimeout(180_000);
			await openPage(page);
			const base = { graph: 'worklet' as const, file: fixture.file, bpm: fixture.bpm, frames };
			const noroll = await render(page, { ...base, rollBeats: 1, subject: 'noroll' });
			const rows: Record<string, number>[] = [];
			for (const rollBeats of ROLL_BEATS) {
				const looped = await render(page, { ...base, rollBeats, subject: 'looped' });
				const hard = await render(page, { ...base, rollBeats, subject: 'hard' });
				const blend = await render(page, { ...base, rollBeats, subject: 'blend' });
				const forced = await render(page, { ...base, rollBeats, subject: 'forced' });
				assertWindowClean(blend, frames);
				const forcedRatio = worstClickRatio(forced, looped, noroll);
				WORKLET_WORST.set('forced', Math.max(WORKLET_WORST.get('forced') ?? 0, forcedRatio));
				rows.push({
					roll: rollBeats,
					hard: round2(worstClickRatio(hard, looped, noroll)),
					blend: round2(worstClickRatio(blend, looped, noroll)),
					forced: round2(forcedRatio),
					blendVsHardDbfs: round2(nullDbfs(blend, hard, RELEASE_FRAME)),
					forcedLeakDbfs: round2(nullDbfs(forced, hard, RELEASE_FRAME + frames)),
					hardVsNorollDbfs: round2(nullDbfs(hard, noroll, RELEASE_FRAME + frames))
				});
			}
			console.log(`worklet ${fixture.file}`);
			console.table(rows);
			for (const row of rows) {
				expect(
					row.blend,
					`if slip return click ratio > 1.5 then broken (roll ${row.roll})`
				).toBeLessThanOrEqual(SLIP_RETURN_CLICK_VETO);
				expect(
					row.blendVsHardDbfs,
					`if the production release differs from the worklet's own seek then broken (roll ${row.roll})`
				).toBe(-Infinity);
				expect(
					row.forcedLeakDbfs,
					`if the old tail leaks past the window then broken (roll ${row.roll})`
				).toBeLessThanOrEqual(NULL_DBFS_MAX);
			}
			const first = await render(page, { ...base, rollBeats: 1, subject: 'blend' });
			const again = await render(page, { ...base, rollBeats: 1, subject: 'blend' });
			expect(
				again,
				'if two identical renders differ then the instrument is not repeatable'
			).toEqual(first);
		});
	}

	test('the chosen length is the shortest candidate that passes on every raw fixture x roll', () => {
		const table = [...RAW_WORST].map(([length, worst]) => ({ length, worst: round2(worst) }));
		console.table(table);
		expect(RAW_WORST.size, 'if the raw tests did not record every length then broken').toBe(
			SLIP_RETURN_BLEND_CANDIDATES.length + 1
		);
		expect(
			RAW_WORST.get('hard'),
			'if a raw hard cut does not read as a click on any fixture then the detector is blind'
		).toBeGreaterThan(SLIP_RETURN_CLICK_VETO);
		console.log(`worklet forced blend worst ${round2(WORKLET_WORST.get('forced') ?? 0)}`);
		expect(
			WORKLET_WORST.get('forced'),
			'if a buffer-source blend over the worklet does not click then the skip is unproven'
		).toBeGreaterThan(SLIP_RETURN_CLICK_VETO);
		const shortest = SLIP_RETURN_BLEND_CANDIDATES.find(
			(candidate) => (RAW_WORST.get(candidate) ?? Infinity) <= SLIP_RETURN_CLICK_VETO
		);
		expect(shortest, 'if the blend is not the shortest passing length then broken').toBe(
			SLIP_RETURN_BLEND_FRAMES
		);
	});
});
