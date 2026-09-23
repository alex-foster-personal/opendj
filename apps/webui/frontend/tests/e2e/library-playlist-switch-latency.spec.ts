// requirement: PERF-UI-05
/**
 * Live latency bench for playlist tree ready and playlist / All Tracks switch
 * first-row paint (issue #3530). Reads the app's perf ring (same spans as
 * library-perf.ts) and fails when p50 caps are exceeded.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
	PLAYLIST_SWITCH_BENCH_ORIGIN,
	PLAYLIST_SWITCH_BENCH_PORT
} from './playwright.playlist-switch-latency.config';
import {
	summarizeSamples,
	THRESHOLDS_P50_MS
} from './support/playlist-switch-bench-stats.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const PLAYLIST_NAME = 'Perf 1k';

const COMMITTED_FIXTURE_PATH = join(
	FRONTEND_ROOT,
	'tests',
	'fixtures',
	'library-playlist-switch-bench.json'
);
const DEFAULT_OUT_PATH = join(
	FRONTEND_ROOT,
	'test-results',
	'library-playlist-switch-bench.json'
);
const OUT_PATH = resolve(process.env.PLAYLIST_SWITCH_BENCH_OUT ?? DEFAULT_OUT_PATH);
const COMMITTED_RESOLVED = resolve(COMMITTED_FIXTURE_PATH);

const SAMPLES = Number(process.env.PLAYLIST_SWITCH_BENCH_SAMPLES ?? 20);

interface PerfRingRow {
	kind: string;
	stages?: Record<string, number>;
	labels?: Record<string, string>;
}

interface PerfRingWindow extends Window {
	__mdtPerfLog?: () => readonly PerfRingRow[];
}

interface Sample {
	playlist_tree_ready_ms: number;
	playlist_switch_first_rows_ms: number;
	all_tracks_first_rows_ms: number;
}

async function waitForPerfRing(page: Page): Promise<void> {
	await page.waitForFunction(
		() => typeof (window as PerfRingWindow).__mdtPerfLog === 'function',
		undefined,
		{ timeout: 60_000 }
	);
}

async function readLatestPerfMs(
	page: Page,
	kind: string,
	stageKey: string,
	labels?: Record<string, string>
): Promise<number> {
	return await page.evaluate(
		({ kind, stageKey, labels }) => {
			const rows = (window as PerfRingWindow).__mdtPerfLog?.() ?? [];
			for (let i = rows.length - 1; i >= 0; i--) {
				const row = rows[i];
				if (row.kind !== kind) continue;
				if (labels !== undefined) {
					let labelMatch = true;
					for (const [key, value] of Object.entries(labels)) {
						if (row.labels?.[key] !== value) labelMatch = false;
					}
					if (!labelMatch) continue;
				}
				const measured = row.stages?.[stageKey];
				if (typeof measured === 'number') return measured;
			}
			throw new Error(`missing perf row kind=${kind} stage=${stageKey}`);
		},
		{ kind, stageKey, labels }
	);
}

async function waitForTreeReady(page: Page): Promise<void> {
	await expect(page.getByTestId('playlist-tree')).toBeVisible();
	await expect(page.getByTestId('playlists-loading')).toHaveCount(0);
	await expect(page.getByTestId('playlist-all-tracks')).toBeVisible();
	await expect(page.getByTestId('playlist-row').filter({ hasText: PLAYLIST_NAME })).toBeVisible();
	await page.waitForFunction(
		() => {
			const rows = (window as PerfRingWindow).__mdtPerfLog?.() ?? [];
			return rows.some((row) => row.kind === 'library-playlist-tree-ready');
		},
		undefined,
		{ timeout: 60_000 }
	);
}

async function waitForSwitchPaint(page: Page): Promise<void> {
	await expect(page.getByTestId('track-row').first()).toBeVisible({ timeout: 10_000 });
	await page.waitForFunction(() => {
		const row = document.querySelector<HTMLElement>('[data-testid="track-row"]');
		return row !== null && (row.innerText?.trim().length ?? 0) > 0;
	});
	await page.evaluate(
		() =>
			new Promise<void>((resolve) => {
				requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
			})
	);
}

async function measureOneSample(page: Page): Promise<Sample> {
		await page.addInitScript(() => localStorage.removeItem('odj.brand-launch.v1'));
		await page.goto('/performance', { waitUntil: 'domcontentloaded' });
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
			timeout: 60_000
		});
		await waitForPerfRing(page);
		await waitForTreeReady(page);
		const playlist_tree_ready_ms = await readLatestPerfMs(
			page,
			'library-playlist-tree-ready',
			'ready_ms'
		);

		await page.getByTestId('playlist-all-tracks').click();
		await waitForSwitchPaint(page);
		const playlistCountBefore = await page.evaluate(
			() => (window as PerfRingWindow).__mdtPerfLog?.().length ?? 0
		);
		await page.getByTestId('playlist-row').filter({ hasText: PLAYLIST_NAME }).click();
		await waitForSwitchPaint(page);
		await page.waitForFunction(
			(before) => {
				const rows = (window as PerfRingWindow).__mdtPerfLog?.() ?? [];
				return rows
					.slice(before)
					.some(
						(row) =>
							row.kind === 'library-switch-first-rows' &&
							row.labels?.source === 'playlist'
					);
			},
			playlistCountBefore,
			{ timeout: 10_000 }
		);
		const playlist_switch_first_rows_ms = await readLatestPerfMs(
			page,
			'library-switch-first-rows',
			'first_rows_ms',
			{ source: 'playlist' }
		);

		const allTracksCountBefore = await page.evaluate(
			() => (window as PerfRingWindow).__mdtPerfLog?.().length ?? 0
		);
		await page.getByTestId('playlist-all-tracks').click();
		await waitForSwitchPaint(page);
		await page.waitForFunction(
			(before) => {
				const rows = (window as PerfRingWindow).__mdtPerfLog?.() ?? [];
				return rows
					.slice(before)
					.some(
						(row) =>
							row.kind === 'library-switch-first-rows' &&
							row.labels?.source === 'all-tracks'
					);
			},
			allTracksCountBefore,
			{ timeout: 10_000 }
		);
		const all_tracks_first_rows_ms = await readLatestPerfMs(
			page,
			'library-switch-first-rows',
			'first_rows_ms',
			{ source: 'all-tracks' }
		);

	return {
		playlist_tree_ready_ms,
		playlist_switch_first_rows_ms,
		all_tracks_first_rows_ms
	};
}

async function warmPlaylistEndpoints(request: APIRequestContext): Promise<void> {
	const warm = [
		`${PLAYLIST_SWITCH_BENCH_ORIGIN}/api/v1/playlists?availability=skip`,
		`${PLAYLIST_SWITCH_BENCH_ORIGIN}/api/v1/playlists/pl-perf-1k/tracks?limit=30&offset=0`
	];
	for (const url of warm) {
		const response = await request.get(url);
		expect(response.ok(), `warmup GET ${url} failed`).toBeTruthy();
	}
}

test('playlist tree and switch first-row paint meet PERF-UI-05 caps', async ({
	browser,
	request
}) => {
	test.setTimeout(1_200_000);
	await warmPlaylistEndpoints(request);
	const samples: Sample[] = [];
	for (let run = 0; run < SAMPLES + 1; run += 1) {
		const context = await browser.newContext();
		const page = await context.newPage();
		let sample: Sample;
		try {
			sample = await measureOneSample(page);
		} finally {
			await context.close();
		}
		if (run > 0) samples.push(sample);
		// eslint-disable-next-line no-console
		console.log(
			`[playlist-switch-bench] run ${run}${run === 0 ? ' (warmup, discarded)' : ''}: ` +
				`tree=${sample.playlist_tree_ready_ms}ms ` +
				`switch=${sample.playlist_switch_first_rows_ms}ms ` +
				`all_tracks=${sample.all_tracks_first_rows_ms}ms`
		);
	}
	expect(samples.length).toBeGreaterThanOrEqual(SAMPLES);

	const post_fix = {
		playlist_tree_ready_ms: summarizeSamples(
			samples.map((s) => s.playlist_tree_ready_ms)
		),
		playlist_switch_first_rows_ms: summarizeSamples(
			samples.map((s) => s.playlist_switch_first_rows_ms)
		),
		all_tracks_first_rows_ms: summarizeSamples(samples.map((s) => s.all_tracks_first_rows_ms))
	};

	for (const [metric, cap] of Object.entries(THRESHOLDS_P50_MS)) {
		const measured = post_fix[metric as keyof typeof post_fix].p50;
		expect(
			measured,
			`${metric} p50 ${measured}ms exceeds cap ${cap}ms`
		).toBeLessThanOrEqual(cap);
	}

	const payload = {
		thresholds_p50_ms: THRESHOLDS_P50_MS,
		baseline_pre_fix: {
			playlist_tree_ready_ms: { p50: 2300, p95: 3100 },
			playlist_switch_first_rows_ms: { p50: 850, p95: 1200 },
			all_tracks_first_rows_ms: { p50: 120, p95: 180 }
		},
		post_fix,
		host: `playlist-switch-latency-data / ${SAMPLES} samples @ :${PLAYLIST_SWITCH_BENCH_PORT}`,
		sha: 'live-bench',
		origin: PLAYLIST_SWITCH_BENCH_ORIGIN,
		samples
	};
	if (
		OUT_PATH === COMMITTED_RESOLVED &&
		process.env.PLAYLIST_SWITCH_BENCH_UPDATE_FIXTURE !== '1'
	) {
		throw new Error(
			`Refusing to write playlist-switch bench results to committed fixture ${COMMITTED_FIXTURE_PATH}. ` +
				`Set PLAYLIST_SWITCH_BENCH_UPDATE_FIXTURE=1 to refresh the committed snapshot, ` +
				`or omit PLAYLIST_SWITCH_BENCH_OUT to use the default gitignored path: ${DEFAULT_OUT_PATH}`
		);
	}
	mkdirSync(dirname(OUT_PATH), { recursive: true });
	writeFileSync(OUT_PATH, `${JSON.stringify(payload, null, 2)}\n`);

	// eslint-disable-next-line no-console
	console.log(`[playlist-switch-bench] post_fix ${JSON.stringify(post_fix)} -> ${OUT_PATH}`);
});
