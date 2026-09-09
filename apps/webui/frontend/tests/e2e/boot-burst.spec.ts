/**
 * PERF-R6: what a deck load costs when it is fired inside the boot burst.
 *
 * THE MEASURABLE. `fetchWall` is the engine's own stage timing for the deck
 * load's parallel fetch block (getTrack + audio + anlz + hot cues + stem
 * probe, audio-engine.svelte.ts). It is the stage the A/B against the 18 Aug
 * build convicted: median 3793 -> 8038ms for a load fired at boot, while the
 * same load on a settled page was unchanged at 86ms. So this bench fires the
 * load as early as the performance IPC exists, which is the worst case and
 * the one the maintainer actually meets when he opens the app and grabs a track.
 *
 * WHAT IS BEING COMPARED. Two arms, same machine, same fixture, same engine
 * command: a build BEFORE the boot scheduler and a build AFTER it. Nothing in
 * here knows which arm it is - it writes its samples to BOOT_BURST_OUT and
 * the comparison is arithmetic over the two files.
 *
 * WHY A MEDIAN OF SEVERAL. A single boot is dominated by whatever else the
 * machine was doing. The first run of each arm is a WARMUP and is discarded
 * (the engine's caches, the OS page cache and the fixture's first decode all
 * settle in it); the rest are the sample.
 *
 * Regression lines:
 *  - if the deck load is not fired before the boot requests land then this
 *    measures steady state, which was never the problem
 *  - if fetchWall is absent from the perf ring then the engine stopped
 *    recording its own stages and the number is unanchored
 *  - if the warmup run is counted then the first-boot cost is smeared into
 *    the median of both arms unevenly
 */
import { expect, test, type Browser, type Page } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

// Imported for its `declare global` on window.musicDjToolsPerformance, which
// is how the init script below is type checked against the real IPC.
import type { PerformanceBrowserIpc } from '../../src/lib/rb/performance-ipc.svelte';
import { BOOT_BURST_ORIGIN } from './playwright.boot-burst.config';

/** The DevTools bridge startAppInstruments installs (perf-event-log.ts). */
interface PerfRingWindow extends Window {
	__mdtLastLoads?: (limit?: number) => readonly {
		kind: string;
		t: string;
		stages?: Record<string, number>;
	}[];
	musicDjToolsPerformance?: PerformanceBrowserIpc;
}

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

/** Measured samples, after the discarded warmup. Five is the floor. */
const SAMPLES = Number(process.env.BOOT_BURST_SAMPLES ?? 6);

/**
 * Emulated per-request latency, in ms. 0 (the default) measures the raw
 * loopback regime.
 *
 * WHY THIS KNOB EXISTS, stated plainly so nobody reads a modelled number as a
 * measured one. The fixture library is two generated tracks, so its whole
 * boot burst is ~86ms - about forty times smaller than the 3579ms the maintainer's real
 * 8000-row library produces. At that scale the contention this bench is about
 * is well under the machine's own run-to-run noise, and fetchWall cannot
 * resolve it. Adding a flat per-request latency does NOT invent data: it puts
 * every request, in BOTH arms, into a regime where the origin's six
 * connections and the daemon's single worker are the binding constraint,
 * which is the regime the finding was made in. It is a model of request cost,
 * declared as one, applied identically to both arms.
 */
const LATENCY_MS = Number(process.env.BOOT_BURST_LATENCY_MS ?? 0);

/** Where the arm writes its raw samples for the A/B arithmetic. */
const OUT_PATH =
	process.env.BOOT_BURST_OUT ?? join(FRONTEND_ROOT, 'tests', 'e2e', 'fixtures', 'boot-burst.json');

/**
 * The endpoint families this page opens at startup that a deck load has to
 * share the origin's six connections with. Verbatim from the PERF-R6
 * finding, so the "how many are still in the burst" count is comparable
 * across arms.
 */
const BOOT_FAMILIES: readonly string[] = [
	'/api/v1/auth/me',
	'/api/v1/entitlements',
	'/api/v1/build-info',
	'/api/v1/feedback/comments',
	'/api/v1/feedback/general',
	'/api/v1/feedback/todos',
	'/api/v1/jobs',
	'/api/v1/setup/status',
	'/api/v1/telemetry/heartbeat'
];

/**
 * How long the app's startup request wave lasts, measured FROM ITS OWN
 * FIRST REQUEST rather than from navigation start.
 *
 * The anchor matters and was got wrong once: under the production bundle
 * plus emulated latency, hydration does not begin until ~1450ms into the
 * page, so a window anchored at navigation start closed before the app had
 * booted and reported zero duplicates in BOTH arms -- a clean binary answer
 * to a question that should have been messy, which is the instrument
 * failing rather than a result. Anchored to the wave itself, the count is
 * immune to how long the bundle and the emulated latency take to get there.
 *
 * The two mount waves (+layout, then BrowserPanel) run 77-230ms apart, so
 * 1000ms contains both with room to spare while staying well under the
 * 2500ms library liveness cadence. That cadence shares the
 * `/api/v1/health` URL and is deliberately NOT coalesced (see
 * `src/lib/api/request-coalescer.ts`), so the health count is always
 * "body reads + 1" in BOTH arms: the bench cannot tell a ping from a body
 * read, they are the same URL, and it does not need to.
 */
const BOOT_WINDOW_MS = 1000;

/** The endpoints whose boot-time duplicates the coalescer exists to remove. */
const DEDUPED_AT_BOOT: readonly string[] = ['/api/v1/health', '/api/v1/ui-prefs'];

interface ApiTiming {
	name: string;
	start: number;
	end: number;
}

interface Sample {
	/** The deck load's own parallel-fetch stage, in ms. THE measurable. */
	fetchWall: number;
	/** The whole deck-load event, in ms. */
	deckLoadMs: number;
	/** Requests from BOOT_FAMILIES that were still open while the deck load
	 * was fetching: the contention this change is meant to remove. */
	contendingCalls: number;
	/** Span from the first boot-family request starting to the last one
	 * ending, in ms. The "boot burst" of the finding. */
	burstSpanMs: number;
	/** When the deck load's fetch block finished, on the page clock. */
	deckFetchEndMs: number;
	/** `/api/v1/health` requests inside BOOT_WINDOW_MS. Body reads plus the
	 * one liveness ping that always falls in the window. */
	healthCallsAtBoot: number;
	/** `/api/v1/ui-prefs` requests inside BOOT_WINDOW_MS. */
	uiPrefsCallsAtBoot: number;
}

function _median(values: readonly number[]): number {
	const sorted = [...values].sort((a, b) => a - b);
	const mid = Math.floor(sorted.length / 2);
	return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/** Two ids from the fixture library, read OUTSIDE the page so the read is
 * not itself part of the boot burst being measured. */
async function _fixtureTrackIds(browser: Browser): Promise<string[]> {
	const context = await browser.newContext();
	try {
		const response = await context.request.get(`${BOOT_BURST_ORIGIN}/api/v1/tracks?limit=2`);
		expect(response.ok(), `GET /api/v1/tracks failed: ${response.status()}`).toBe(true);
		const body = (await response.json()) as { items?: { stable_id?: unknown }[] };
		const ids = (body.items ?? [])
			.map((row) => row.stable_id)
			.filter((id): id is string => typeof id === 'string');
		expect(ids.length, 'the fixture library must hold at least one track').toBeGreaterThan(0);
		return ids;
	} finally {
		await context.close();
	}
}

/**
 * One boot: open the page and fire a deck load the instant the IPC exists.
 *
 * The load is dispatched from an init script rather than from the test, so
 * it starts at the earliest moment the app can accept it instead of after a
 * round trip to the Playwright process - which is what "a deck loaded while
 * the app is still booting" actually means.
 */
async function _measureOneBoot(page: Page, stableId: string): Promise<Sample> {
	if (LATENCY_MS > 0) {
		const cdp = await page.context().newCDPSession(page);
		await cdp.send('Network.enable');
		await cdp.send('Network.emulateNetworkConditions', {
			offline: false,
			latency: LATENCY_MS,
			// Throughput is deliberately unthrottled: the mechanism under test
			// is per-request cost against a six-connection pool, not bandwidth.
			downloadThroughput: -1,
			uploadThroughput: -1
		});
	}

	await page.addInitScript((id: string) => {
		const started = performance.now();
		const fire = (): void => {
			const ipc = (window as PerfRingWindow).musicDjToolsPerformance;
			if (ipc !== undefined && ipc.version === 1) {
				void ipc.dispatch({ type: 'load', deck: 1, stable_id: id });
				return;
			}
			// 30s is the other suites' IPC ceiling; past it the page is broken
			// and the wait below says so rather than this loop spinning on.
			if (performance.now() - started > 30_000) return;
			setTimeout(fire, 0);
		};
		fire();
	}, stableId);

	await page.goto('/performance');

	// The deck-load event lands in the engine's own perf ring. Waiting on the
	// ring rather than on a pixel means the number is the engine's, not a
	// paint time that happens to correlate with it.
	await page.waitForFunction(
		() => {
			const ring = (window as PerfRingWindow).__mdtLastLoads?.(8) ?? [];
			return ring.some((event) => typeof event.stages?.fetchWall === 'number');
		},
		undefined,
		{ timeout: 180_000 }
	);

	interface EvalArgs {
		families: readonly string[];
		bootWindowMs: number;
		deduped: readonly string[];
	}
	return page.evaluate(({ families, bootWindowMs, deduped }: EvalArgs) => {
		const ring = (window as PerfRingWindow).__mdtLastLoads?.(8) ?? [];
		const load = ring.find((event) => typeof event.stages?.fetchWall === 'number');
		if (load?.stages === undefined) throw new Error('no deck-load event carrying fetchWall');
		const fetchWall = load.stages.fetchWall;
		const deckLoadMs = load.stages.total ?? fetchWall;

		const api = (
			performance.getEntriesByType('resource') as PerformanceResourceTiming[]
		)
			.filter((entry) => entry.name.includes('/api/v1/'))
			.map((entry) => ({ name: entry.name, start: entry.startTime, end: entry.responseEnd }));

		const boot = api.filter((entry) => families.some((family) => entry.name.includes(family)));
		const burstSpanMs =
			boot.length === 0
				? 0
				: Math.max(...boot.map((e) => e.end)) - Math.min(...boot.map((e) => e.start));

		// The ring row is stamped in wall clock; both halves come from this
		// page, so subtracting timeOrigin puts it on the same clock as the
		// resource timings. The load ended when the row was written, so its
		// fetch block ran over [end - total, end - total + fetchWall].
		const deckEndMs = new Date(load.t).getTime() - performance.timeOrigin;
		const deckStartMs = deckEndMs - deckLoadMs;
		const deckFetchEndMs = deckStartMs + fetchWall;
		const contendingCalls = boot.filter(
			(entry) => entry.start < deckFetchEndMs && entry.end > deckStartMs
		).length;

		// Counted over the app's own startup wave, NOT relative to the deck
		// load: these are duplicates the boot issues whether or not a deck is
		// loading. The wave starts at the first request to any of these
		// endpoints; if the app never made one the window is empty and the
		// counts are -1, never a zero that would read as "deduplicated".
		const dedupedStarts = api
			.filter((entry) => deduped.some((path) => entry.name.includes(path)))
			.map((entry) => entry.start);
		const waveStart = dedupedStarts.length === 0 ? null : Math.min(...dedupedStarts);
		const inBootWindow = (path: string): number =>
			waveStart === null
				? -1
				: api.filter(
						(entry) =>
							entry.name.includes(path) &&
							entry.start >= waveStart &&
							entry.start < waveStart + bootWindowMs
					).length;

		return {
			fetchWall,
			deckLoadMs,
			contendingCalls,
			burstSpanMs,
			deckFetchEndMs,
			healthCallsAtBoot: inBootWindow('/api/v1/health'),
			uiPrefsCallsAtBoot: inBootWindow('/api/v1/ui-prefs')
		};
	}, { families: BOOT_FAMILIES, bootWindowMs: BOOT_WINDOW_MS, deduped: DEDUPED_AT_BOOT });
}

test('a deck load fired at boot, measured over repeated cold page loads', async ({ browser }) => {
	const ids = await _fixtureTrackIds(browser);
	const stableId = ids[0];

	const samples: Sample[] = [];
	// One warmup plus the measured runs. Each boot gets a FRESH context, so
	// the browser cache cannot make run five look like an improvement.
	for (let run = 0; run < SAMPLES + 1; run += 1) {
		const context = await browser.newContext();
		const page = await context.newPage();
		try {
			const sample = await _measureOneBoot(page, stableId);
			if (run > 0) samples.push(sample);
			// eslint-disable-next-line no-console
			console.log(
				`[boot-burst] run ${run}${run === 0 ? ' (warmup, discarded)' : ''}: ` +
					`fetchWall=${Math.round(sample.fetchWall)}ms ` +
					`deckLoad=${Math.round(sample.deckLoadMs)}ms ` +
					`contending=${sample.contendingCalls} ` +
					`burstSpan=${Math.round(sample.burstSpanMs)}ms ` +
					`health@boot=${sample.healthCallsAtBoot} ` +
					`uiPrefs@boot=${sample.uiPrefsCallsAtBoot}`
			);
		} finally {
			await context.close();
		}
	}

	expect(samples.length).toBeGreaterThanOrEqual(5);

	const report = {
		origin: BOOT_BURST_ORIGIN,
		emulatedLatencyMs: LATENCY_MS,
		samples,
		median: {
			fetchWall: _median(samples.map((s) => s.fetchWall)),
			deckLoadMs: _median(samples.map((s) => s.deckLoadMs)),
			contendingCalls: _median(samples.map((s) => s.contendingCalls)),
			burstSpanMs: _median(samples.map((s) => s.burstSpanMs)),
			healthCallsAtBoot: _median(samples.map((s) => s.healthCallsAtBoot)),
			uiPrefsCallsAtBoot: _median(samples.map((s) => s.uiPrefsCallsAtBoot))
		}
	};
	mkdirSync(dirname(OUT_PATH), { recursive: true });
	writeFileSync(OUT_PATH, `${JSON.stringify(report, null, '\t')}\n`);

	// eslint-disable-next-line no-console
	console.log(`[boot-burst] MEDIANS ${JSON.stringify(report.median)} -> ${OUT_PATH}`);
});
