/**
 * PERF-LEAK-11717 (#10278, epic #9376): headless load, play, unload leak-cycle probe.
 *
 * For LEAK_CYCLES (default 12) cycles on the deckload fixture library this spec
 * loads the two fixture tracks onto decks 1-4 through window.musicDjToolsPerformance,
 * plays 5 s, stops, unloads all four, then forces GC over CDP and records
 * Runtime.getHeapUsage, window.__mdtLiveHandles() totals and the page canvas count.
 * Every cycle is written to the result file (LEAK_CYCLE_OUT, default
 * test-results/leak-cycle-probe.json). The verdict belongs to the reducer:
 *
 *   pnpm exec playwright test --config tests/e2e/playwright.performance.config.ts leak-cycle-probe.spec.ts
 *   uv run python -m scripts.perf.leak_cycle_report --result apps/webui/frontend/test-results/leak-cycle-probe.json --json
 *
 * [if] a cycle does not load, play and unload all four decks [then] fail, [else stop].
 * [if] the result file lacks a reading for any requested cycle [then] fail, [else stop].
 *
 * WebKit (and Firefox) are skipped: no CDP heap API. live_handles is null until
 * #10275 ships window.__mdtLiveHandles().
 */
import { expect, test } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { DeckId } from '../../src/lib/rb/deck-slots';
import { API_BASE, dispatch, gotoPerformance, query, waitAudible } from './support/beat-sync-e2e';

const LEAK_CYCLES = Number(process.env.LEAK_CYCLES ?? 12);
const PLAY_MS = 5_000;
const TRACK_TITLES = ['webkit-fixture-a-128bpm', 'webkit-fixture-b-124bpm'] as const;
const DECKS: readonly DeckId[] = [1, 2, 3, 4];
const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const OUT_PATH =
	process.env.LEAK_CYCLE_OUT ?? join(FRONTEND_ROOT, 'test-results', 'leak-cycle-probe.json');
const FIXTURE_MODE = process.env.PERFORMANCE_E2E_FIXTURE !== '0';
const MIB = 1024 * 1024;

interface CycleReading {
	cycle: number;
	js_heap_used_mb: number;
	js_heap_total_mb: number;
	live_handles: Record<string, number> | null;
	live_handle_sites: unknown;
	canvas_count: number;
	t_ms: number;
}

test.use({
	launchOptions: { args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio'] }
});

test('load, play, unload cycles record heap, live handles and canvases per cycle', async ({
	page,
	request,
	browserName
}) => {
	test.skip(
		browserName !== 'chromium',
		'CDP heap API is Chromium only; WebKit has none (PERF-LEAK-11717)'
	);
	test.skip(!FIXTURE_MODE, 'requires the performance Playwright fixture library');
	expect(Number.isInteger(LEAK_CYCLES) && LEAK_CYCLES >= 1, 'LEAK_CYCLES').toBe(true);
	test.setTimeout(60_000 + LEAK_CYCLES * 45_000);
	const started = Date.now();

	const listing = await request.get(`${API_BASE}/api/v1/tracks?limit=1000&available=true`);
	expect(listing.ok(), 'track listing').toBeTruthy();
	const items = ((await listing.json()) as { items: { stable_id: string; title: string | null }[] })
		.items;
	const tracks = TRACK_TITLES.map((title) => {
		const row = items.find((item) => item.title === title);
		test.skip(row === undefined, `fixture track ${title} is not in this library`);
		return { title, stable_id: row!.stable_id };
	});
	const expected = (deck: DeckId): string => tracks[deck % 2 === 1 ? 0 : 1].stable_id;

	await gotoPerformance(page, '?perfprobe=1');
	const cdp = await page.context().newCDPSession(page);
	const cycles: CycleReading[] = [];

	for (let cycle = 1; cycle <= LEAK_CYCLES; cycle++) {
		for (const deck of DECKS) {
			await dispatch(page, { type: 'load', deck, stable_id: expected(deck) });
		}
		await expect
			.poll(
				async () => {
					const state = await query(page);
					return DECKS.every((deck) => state.decks[deck].stable_id === expected(deck));
				},
				{ message: `cycle ${cycle} load` }
			)
			.toBe(true);
		for (const deck of DECKS) await dispatch(page, { type: 'play', deck, playing: true });
		for (const deck of DECKS) await waitAudible(page, deck);
		await page.waitForTimeout(PLAY_MS);
		for (const deck of DECKS) await dispatch(page, { type: 'play', deck, playing: false });
		for (const deck of DECKS) await dispatch(page, { type: 'unload', deck });
		await expect
			.poll(
				async () => {
					const state = await query(page);
					return DECKS.every((deck) => state.decks[deck].stable_id === null);
				},
				{ message: `cycle ${cycle} unload` }
			)
			.toBe(true);

		await cdp.send('HeapProfiler.collectGarbage');
		await cdp.send('HeapProfiler.collectGarbage');
		const usage = await cdp.send('Runtime.getHeapUsage');
		const raw = await page.evaluate(() => {
			const probe = (window as unknown as { __mdtLiveHandles?: () => Record<string, unknown> })
				.__mdtLiveHandles;
			return probe === undefined ? null : probe();
		});
		let liveHandles: Record<string, number> | null = null;
		let liveSites: unknown = null;
		if (raw !== null && typeof raw === 'object') {
			liveHandles = {};
			for (const [kind, value] of Object.entries(raw)) {
				if (typeof value === 'number' && Number.isFinite(value)) liveHandles[kind] = value;
			}
			const sites = raw.by_site;
			liveSites = sites !== null && typeof sites === 'object' ? sites : null;
		}
		const canvasCount = await page.evaluate(() => document.querySelectorAll('canvas').length);
		cycles.push({
			cycle,
			js_heap_used_mb: usage.usedSize / MIB,
			js_heap_total_mb: usage.totalSize / MIB,
			live_handles: liveHandles,
			live_handle_sites: liveSites,
			canvas_count: canvasCount,
			t_ms: Date.now() - started
		});
		mkdirSync(dirname(OUT_PATH), { recursive: true });
		writeFileSync(
			OUT_PATH,
			JSON.stringify(
				{
					schema: 'leak-cycle-probe/1',
					browser: 'chromium',
					cycles_requested: LEAK_CYCLES,
					play_ms: PLAY_MS,
					tracks,
					live_handles_available: cycles.some((row) => row.live_handles !== null),
					cycles
				},
				null,
				2
			)
		);
	}

	expect(cycles).toHaveLength(LEAK_CYCLES);
	console.log(`leak-cycle-probe: wrote ${cycles.length} cycles to ${OUT_PATH}`);
});
