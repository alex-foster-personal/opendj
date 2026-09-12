/**
 * Stem decode bench: four-way FLAC decode on the production browser path.
 *
 * THE MEASURABLE is decodeStems on the deck-stems perf ring row emitted after
 * a mix load settles and the lazy stem upgrade runs against a real demucs4
 * bundle served by the engine.
 *
 * MEASUREMENT lane: medians are recorded, never compared to a threshold. A red
 * run means the harness broke or the fixture was unreachable (UNKNOWN), not
 * that decode got slower.
 */
import { expect, test, type Browser, type Page } from '@playwright/test';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { execSync } from 'node:child_process';
import { hostname } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { PerformanceBrowserIpc } from '../../src/lib/rb/performance-ipc.svelte';
import {
	FIXTURE_MANIFEST,
	STEM_DECODE_BENCH_ORIGIN
} from './playwright.stem-decode-bench.config';

interface PerfRingWindow extends Window {
	__mdtPerfLog?: () => readonly {
		kind: string;
		t: string;
		stages?: Record<string, number>;
		labels?: Record<string, string>;
	}[];
	musicDjToolsPerformance?: PerformanceBrowserIpc;
}

interface FixtureManifest {
	stable_id?: string;
	denominator?: string;
}

interface Sample {
	decodeStemsMs: number;
	totalMs: number;
	stemDecode: string | null;
	stemDecodeWorkers: string | null;
}

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const SAMPLES = Number(process.env.STEM_DECODE_BENCH_SAMPLES ?? 6);
const OUT_PATH =
	process.env.STEM_DECODE_BENCH_OUT ??
	join(FRONTEND_ROOT, 'tests', 'e2e', 'fixtures', 'stem-decode-bench.json');

function _median(values: readonly number[]): number {
	const sorted = [...values].sort((a, b) => a - b);
	const mid = Math.floor(sorted.length / 2);
	return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function _readFixtureManifest(): FixtureManifest {
	if (!existsSync(FIXTURE_MANIFEST)) {
		throw new Error(
			`UNKNOWN: fixture manifest missing at ${FIXTURE_MANIFEST}; stem decode bench cannot run`
		);
	}
	return JSON.parse(readFileSync(FIXTURE_MANIFEST, 'utf8')) as FixtureManifest;
}

async function _fixtureTrackId(browser: Browser, manifest: FixtureManifest): Promise<string> {
	if (typeof manifest.stable_id === 'string' && manifest.stable_id.length > 0) {
		return manifest.stable_id;
	}
	const context = await browser.newContext();
	try {
		const response = await context.request.get(`${STEM_DECODE_BENCH_ORIGIN}/api/v1/tracks?limit=1`);
		expect(response.ok(), `GET /api/v1/tracks failed: ${response.status()}`).toBe(true);
		const body = (await response.json()) as { items?: { stable_id?: unknown }[] };
		const stableId = body.items?.[0]?.stable_id;
		expect(typeof stableId, 'fixture library must hold one track').toBe('string');
		return stableId as string;
	} finally {
		await context.close();
	}
}

async function _measureOneLoad(page: Page, stableId: string): Promise<Sample> {
	await page.goto('/performance');
	await page.waitForFunction(
		() => (window as PerfRingWindow).musicDjToolsPerformance?.version === 1,
		undefined,
		{ timeout: 60_000 }
	);
	await page.evaluate((id: string) => {
		const ipc = (window as PerfRingWindow).musicDjToolsPerformance;
		void ipc?.dispatch({ type: 'load', deck: 1, stable_id: id });
	}, stableId);

	await page.waitForFunction(
		() => {
			const rows = (window as PerfRingWindow).__mdtPerfLog?.() ?? [];
			return rows.some(
				(row) =>
					row.kind.startsWith('deck-stems') &&
					typeof row.stages?.decodeStems === 'number' &&
					row.stages.decodeStems >= 0
			);
		},
		undefined,
		{ timeout: 180_000 }
	);

	return page.evaluate(() => {
		const rows = (window as PerfRingWindow).__mdtPerfLog?.() ?? [];
		const stemRow = [...rows]
			.reverse()
			.find(
				(row) =>
					row.kind.startsWith('deck-stems') &&
					typeof row.stages?.decodeStems === 'number'
			);
		if (stemRow?.stages === undefined) {
			throw new Error('harness broke: deck-stems row missing decodeStems');
		}
		return {
			decodeStemsMs: stemRow.stages.decodeStems,
			totalMs: stemRow.stages.total ?? stemRow.stages.decodeStems,
			stemDecode: stemRow.labels?.stem_decode ?? null,
			stemDecodeWorkers: stemRow.labels?.stem_decode_workers ?? null
		};
	});
}

test('four-way stem decode on a demucs4 FLAC bundle', async ({ browser }) => {
	const manifest = _readFixtureManifest();
	const stableId = await _fixtureTrackId(browser, manifest);
	const samples: Sample[] = [];

	for (let run = 0; run < SAMPLES + 1; run += 1) {
		const context = await browser.newContext();
		const page = await context.newPage();
		try {
			const sample = await _measureOneLoad(page, stableId);
			if (run > 0) samples.push(sample);
			console.log(
				`[stem-decode-bench] run ${run}${run === 0 ? ' (warmup, discarded)' : ''}: ` +
					`decodeStems=${Math.round(sample.decodeStemsMs)}ms ` +
					`total=${Math.round(sample.totalMs)}ms ` +
					`lane=${sample.stemDecode ?? 'unknown'} ` +
					`workers=${sample.stemDecodeWorkers ?? 'unknown'}`
			);
		} finally {
			await context.close();
		}
	}

	expect(samples.length).toBeGreaterThanOrEqual(5);
	for (const sample of samples) {
		expect(sample.decodeStemsMs, 'decodeStems must be present on every sample').toBeGreaterThan(0);
	}

	const report = {
		host: hostname(),
		git_sha: execSync('git rev-parse HEAD', { cwd: join(FRONTEND_ROOT, '..', '..', '..') })
			.toString()
			.trim(),
		origin: STEM_DECODE_BENCH_ORIGIN,
		stable_id: stableId,
		denominator:
			manifest.denominator ??
			'one demucs4 FLAC bundle decoded through decodeStems after a mix load',
		samples,
		median: {
			decodeStemsMs: _median(samples.map((sample) => sample.decodeStemsMs)),
			totalMs: _median(samples.map((sample) => sample.totalMs))
		},
		noise_note:
			'Same SHA and host: expect decodeStems medians within roughly 20% run-to-run on a quiet nucbox-wsl runner'
	};
	mkdirSync(dirname(OUT_PATH), { recursive: true });
	writeFileSync(OUT_PATH, `${JSON.stringify(report, null, '\t')}\n`);
	console.log(`[stem-decode-bench] MEDIANS ${JSON.stringify(report.median)} -> ${OUT_PATH}`);
});
