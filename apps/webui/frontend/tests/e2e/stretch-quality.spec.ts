/**
 * Stretch-quality render spec.
 *
 * TWO MODES, one spec, because the render path is inherently a browser path
 * and a second copy of it would be a second thing to keep true:
 *
 *  - SMOKE (default, CI-runnable): one fixture, one condition, two arms. It
 *    proves the whole render path works -- interception, decode, non-silence,
 *    configure, determinism, channel distinctness -- in a few seconds, and
 *    needs no daemon because it synthesises its own fixture.
 *  - FULL GRID (`STRETCH_QUALITY_PLAN=<plan.json>`): every fixture x condition
 *    x arm, writing PCM renders and a manifest for the Python analysis half.
 *    Driven by `just stretch-quality`, never by CI.
 */
import { expect, test } from '@playwright/test';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

import {
	installRenderHelpers,
	interceptSyntheticOrigin,
	SAMPLE_RATE_HZ,
	SYNTHETIC_ORIGIN,
	writePcm,
	type CellRender,
	type ExcerptResult,
	type RenderPlan
} from './support/stretch-quality';

const planPath = process.env.STRETCH_QUALITY_PLAN;

/** A 10 s excerpt renders in well under a second; 60 s means it is not coming back. */
const CELL_TIMEOUT_MS = 60_000;

test.describe('stretch quality render path', () => {
	test.describe.configure({ mode: 'serial' });

	test('smoke: one fixture through one condition on two arms', async ({ page }) => {
		test.skip(planPath !== undefined, 'full-grid run supersedes the smoke variant');
		test.setTimeout(180_000);

		// A synthesised fixture, so the smoke test needs no daemon and no
		// library. It is deliberately transient-rich and stereo-distinct: those
		// are the two properties the guards below actually test.
		const plan: RenderPlan = {
			sample_rate_hz: SAMPLE_RATE_HZ,
			out_dir: '',
			fixtures: [],
			arms: [
				{ id: 'baseline', block_ms: null, preset: null, label: 'baseline', is_baseline: true },
				{ id: 'block30', block_ms: 30, preset: null, label: 'blockMs 30' }
			],
			conditions: [{ id: 'C4', rate: 1.08, semitones: 0, role: '+8 percent' }]
		};

		await interceptSyntheticOrigin(page, plan);
		await page.goto(`${SYNTHETIC_ORIGIN}/`);
		await installRenderHelpers(page);

		const excerpt = await page.evaluate((sampleRate) => {
			const scope = window as unknown as Record<string, unknown>;
			const frames = 3 * sampleRate;
			const left = new Float32Array(frames);
			const right = new Float32Array(frames);
			let seed = 12345;
			const random = () => {
				seed = (seed * 1103515245 + 12345) & 0x7fffffff;
				return seed / 0x7fffffff - 0.5;
			};
			for (let frame = 0; frame < frames; frame += 1) {
				const decay = Math.exp(-8 * ((frame % (sampleRate / 4)) / sampleRate));
				left[frame] = random() * decay;
				right[frame] = random() * decay;
			}
			scope.__excerpt = [left, right];
			return { frames, channels: 2 };
		}, SAMPLE_RATE_HZ);
		expect(excerpt.frames).toBe(3 * SAMPLE_RATE_HZ);

		const latencies: Record<string, number> = {};
		for (const arm of plan.arms) {
			const cell = (await page.evaluate(
				async ([sampleRate, blockMs, rate, semitones]) => {
					const scope = window as unknown as Record<string, unknown>;
					const render = scope.__renderCell as (...args: unknown[]) => Promise<CellRender>;
					return render(sampleRate, blockMs, null, rate, semitones, false);
				},
				[SAMPLE_RATE_HZ, arm.block_ms, plan.conditions[0].rate, plan.conditions[0].semitones] as [
					number,
					number | null,
					number,
					number
				]
			)) as CellRender;

			expect(cell.frames, 'round trip returns to the reference length').toBe(excerpt.frames);
			expect(cell.channels).toBe(2);
			// Channel DISTINCTNESS, not channel count: a mono-collapsed render
			// still reports two channels.
			expect(cell.channels_bit_identical, 'render collapsed to mono').toBe(false);
			// Determinism is ENFORCED in the page; this asserts it was enforced.
			expect(cell.deterministic).toBe(true);
			expect(cell.sha256).toBe(cell.repeat_sha256);
			expect(cell.node_latency_ms).toBeGreaterThan(0);
			latencies[arm.id] = cell.node_latency_ms;
		}

		// The self-report is metadata only and is NEVER used to trim a render,
		// but it is the one observable that proves configure() actually took.
		expect(latencies.baseline).toBeCloseTo(120, 0);
		expect(latencies.block30).toBeCloseTo(30, 0);
	});

	test('full grid', async ({ page }) => {
		test.skip(planPath === undefined, 'set STRETCH_QUALITY_PLAN to run the full grid');
		test.setTimeout(3 * 60 * 60_000);

		const plan = JSON.parse(readFileSync(planPath as string, 'utf8')) as RenderPlan;
		await interceptSyntheticOrigin(page, plan);
		await page.goto(`${SYNTHETIC_ORIGIN}/`);
		await installRenderHelpers(page);

		const renderDir = join(plan.out_dir, 'renders');
		const excerptDir = join(plan.out_dir, 'excerpts');
		mkdirSync(renderDir, { recursive: true });
		mkdirSync(excerptDir, { recursive: true });

		// The manifest is written after EVERY fixture and merged into any
		// existing one, keyed by fixture/arm/condition. A ~70 minute grid that
		// only lands its results at the very end loses everything to one kill,
		// and re-running a subset then has to clobber the whole file.
		const manifestPath = join(plan.out_dir, 'renders.json');
		const excerptById = new Map<string, Record<string, unknown>>();
		const cellByKey = new Map<string, Record<string, unknown>>();
		if (existsSync(manifestPath)) {
			const previous = JSON.parse(readFileSync(manifestPath, 'utf8')) as {
				excerpts?: Record<string, unknown>[];
				cells?: Record<string, unknown>[];
			};
			for (const entry of previous.excerpts ?? []) {
				excerptById.set(String(entry.fixture_id), entry);
			}
			for (const entry of previous.cells ?? []) {
				cellByKey.set(`${entry.fixture_id}/${entry.arm_id}/${entry.condition_id}`, entry);
			}
			// eslint-disable-next-line no-console
			console.log(
				`[resume] merging into ${manifestPath}: ${excerptById.size} excerpts, ${cellByKey.size} cells`
			);
		}
		const writeManifest = () => {
			writeFileSync(
				manifestPath,
				`${JSON.stringify(
					{
						sample_rate_hz: plan.sample_rate_hz,
						excerpts: [...excerptById.values()],
						cells: [...cellByKey.values()]
					},
					null,
					2
				)}\n`
			);
		};

		const decodeInto = async (fixture: RenderPlan['fixtures'][number]) =>
			(await page.evaluate(
				async ([id, offset, duration, sampleRate]) => {
					const scope = window as unknown as Record<string, unknown>;
					const decode = scope.__decodeExcerpt as (...args: unknown[]) => Promise<ExcerptResult>;
					return decode(id, offset, duration, sampleRate);
				},
				[fixture.id, fixture.offset_s, fixture.duration_s, plan.sample_rate_hz] as [
					string,
					number,
					number,
					number
				]
			)) as ExcerptResult;

		// Chromium wedges after roughly thirty OfflineAudioContext renders in one
		// document: the next startRendering() simply never resolves, and once it
		// happens EVERY later cell in that page hangs too. Measured twice, at cell
		// 31 and cell 30 of otherwise identical runs. So the page is torn down and
		// rebuilt at each arm boundary, which keeps the budget at nine cells and
		// costs one re-decode. The bounded per-cell wait above is the backstop, not
		// the fix -- abandoning a hung evaluate does not un-wedge the page.
		const freshPage = async (fixture: RenderPlan['fixtures'][number]) => {
			await page.goto(`${SYNTHETIC_ORIGIN}/`);
			await installRenderHelpers(page);
			return decodeInto(fixture);
		};

		for (const fixture of plan.fixtures) {
			const excerpt = await decodeInto(fixture);

			const excerptPath = join(excerptDir, `${fixture.id}.f32`);
			writePcm(excerptPath, excerpt.base64, plan.sample_rate_hz, 2, excerpt.frames);
			excerptById.set(fixture.id, {
				fixture_id: fixture.id,
				material_class: fixture.material_class,
				stable_id: fixture.stable_id,
				bpm: fixture.bpm,
				pitched: fixture.pitched,
				path: excerptPath,
				frames: excerpt.frames,
				rms_dbfs: excerpt.rms_dbfs,
				peak_dbfs: excerpt.peak_dbfs,
				channels_bit_identical: excerpt.channels_bit_identical,
				sha256: excerpt.sha256
			});
			// eslint-disable-next-line no-console
			console.log(
				`[excerpt] ${fixture.id} ${excerpt.frames} frames rms ${excerpt.rms_dbfs.toFixed(1)} dBFS ` +
					`peak ${excerpt.peak_dbfs.toFixed(1)} dBFS`
			);

			for (const arm of plan.arms) {
				const pending = plan.conditions.filter(
					(condition) => cellByKey.get(`${fixture.id}/${arm.id}/${condition.id}`)?.status !== 'ok'
				);
				if (pending.length === 0) {
					// eslint-disable-next-line no-console
					console.log(`[skip] ${fixture.id}/${arm.id} already rendered`);
					continue;
				}
				await freshPage(fixture);

				for (const condition of pending) {
					const label = `${fixture.id}/${arm.id}/${condition.id}`;
					const started = Date.now();
					let cell: CellRender & { forward?: Record<string, unknown> };
					try {
						// A cell that never returns must fail THE CELL, not the grid. One
						// arm/condition pair hung indefinitely in an earlier run and took
						// the whole grid down with it, so the wait is bounded here and a
						// timeout is recorded as a refusal like any other failure.
						cell = (await Promise.race([
							page.evaluate(
								async ([sampleRate, blockMs, preset, rate, semitones, wantForward]) => {
									const scope = window as unknown as Record<string, unknown>;
									const render = scope.__renderCell as (...args: unknown[]) => Promise<CellRender>;
									return render(sampleRate, blockMs, preset, rate, semitones, wantForward);
								},
								[
									plan.sample_rate_hz,
									arm.block_ms,
									arm.preset,
									condition.rate,
									condition.semitones,
									fixture.pitched
								] as [number, number | null, string | null, number, number, boolean]
							),
							new Promise((_resolve, reject) =>
								setTimeout(
									() => reject(new Error(`render exceeded ${CELL_TIMEOUT_MS} ms`)),
									CELL_TIMEOUT_MS
								)
							)
						])) as CellRender & { forward?: Record<string, unknown> };
					} catch (error) {
						cellByKey.set(label, {
							fixture_id: fixture.id,
							arm_id: arm.id,
							condition_id: condition.id,
							status: 'RENDER_FAILED',
							detail: error instanceof Error ? error.message : String(error)
						});
						writeManifest();
						// eslint-disable-next-line no-console
						console.log(`[FAIL] ${label}: ${error}`);
						continue;
					}

					const roundPath = join(renderDir, `${fixture.id}-${arm.id}-${condition.id}.f32`);
					writePcm(roundPath, cell.base64, plan.sample_rate_hz, cell.channels, cell.frames);
					const row: Record<string, unknown> = {
						fixture_id: fixture.id,
						arm_id: arm.id,
						condition_id: condition.id,
						rate: condition.rate,
						semitones: condition.semitones,
						status: 'ok',
						round_trip_path: roundPath,
						round_trip_sha256: cell.sha256,
						repeat_sha256: cell.repeat_sha256,
						deterministic: cell.deterministic,
						frames: cell.frames,
						channels: cell.channels,
						channels_bit_identical: cell.channels_bit_identical,
						node_latency_ms: cell.node_latency_ms,
						render_seconds: (Date.now() - started) / 1000
					};
					if (cell.forward !== undefined) {
						const forwardPath = join(renderDir, `${fixture.id}-${arm.id}-${condition.id}.fwd.f32`);
						writePcm(
							forwardPath,
							cell.forward.base64 as string,
							plan.sample_rate_hz,
							cell.forward.channels as number,
							cell.forward.frames as number
						);
						row.forward_path = forwardPath;
						row.forward_sha256 = cell.forward.sha256;
					}
					cellByKey.set(label, row);
					// Written after EVERY cell, not every fixture: a cell costs well under
					// a second, so the write is free next to losing an interrupted run.
					writeManifest();
					// eslint-disable-next-line no-console
					console.log(
						`[cell] ${label} latency ${cell.node_latency_ms.toFixed(1)} ms ` +
							`sha ${cell.sha256.slice(0, 12)} ${row.render_seconds}s`
					);
				}
			}
			writeManifest();
		}

		writeManifest();
		// eslint-disable-next-line no-console
		console.log(`[manifest] ${manifestPath} (${cellByKey.size} cells)`);
		expect([...cellByKey.values()].filter((cell) => cell.status === 'ok').length).toBeGreaterThan(0);
	});
});
