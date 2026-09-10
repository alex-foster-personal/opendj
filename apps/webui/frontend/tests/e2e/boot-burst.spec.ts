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

	return page.evaluate((families: readonly string[]) => {
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

		return { fetchWall, deckLoadMs, contendingCalls, burstSpanMs, deckFetchEndMs };
	}, BOOT_FAMILIES);
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
					`burstSpan=${Math.round(sample.burstSpanMs)}ms`
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
			burstSpanMs: _median(samples.map((s) => s.burstSpanMs))
		}
	};
	mkdirSync(dirname(OUT_PATH), { recursive: true });
	writeFileSync(OUT_PATH, `${JSON.stringify(report, null, '\t')}\n`);

	// eslint-disable-next-line no-console
	console.log(`[boot-burst] MEDIANS ${JSON.stringify(report.median)} -> ${OUT_PATH}`);
});

/**
 * The conditions half of a timing row, end to end, with nothing injected.
 *
 * The unit suites drive `machine-pressure.ts` with a substituted `fetch` so
 * the unreachable-engine and unreadable-field branches are exercisable at all;
 * this is the other half, and the half a mock cannot give: the real engine
 * answering the real endpoint, the real poller storing it, and a real deck
 * load stamping it onto a real perf-ring row.
 *
 * Asserted as a CONTRACT, not values. The load average of the machine running
 * this is whatever it is; what must hold is that a row either carries a finite
 * reading or carries none, and that `solo` / `concurrent_loads` describe the
 * load that actually happened.
 *
 * Regression lines:
 *  - if the row carries no `solo` then the contention half never reached the ring
 *  - if `pressure_age_ms` is present with no reading beside it then the row
 *    is claiming a measurement it does not have
 *  - if a lone load reports `solo=0` then the span accounting is leaking, which
 *    is the #1658 review finding that made every later row report contention
 */
test('a real deck load stamps the real machine conditions', async ({ browser }) => {
	const ids = await _fixtureTrackIds(browser);
	const context = await browser.newContext();
	const page = await context.newPage();
	try {
		await page.goto('/performance');
		await page.waitForFunction(
			() => (window as PerfRingWindow).musicDjToolsPerformance?.version === 1,
			undefined,
			{ timeout: 60_000 }
		);
		// The endpoint itself, unmocked, before anything reads it second-hand.
		const pressure = (await page.evaluate(async () => {
			const res = await fetch('/api/v1/performance/telemetry/pressure');
			return res.json();
		})) as Record<string, unknown>;
		expect(typeof pressure.available).toBe('boolean');

		// Let the poller take its first sample, then load ONE deck.
		await page.waitForTimeout(2_000);
		await page.evaluate((id: string) => {
			const ipc = (window as PerfRingWindow).musicDjToolsPerformance;
			void ipc?.dispatch({ type: 'load', deck: 1, stable_id: id });
		}, ids[0]);
		await page.waitForFunction(
			() => ((window as PerfRingWindow).__mdtLastLoads?.(8) ?? []).length > 0,
			undefined,
			{ timeout: 120_000 }
		);

		const labels = await page.evaluate(() => {
			const rows = (window as PerfRingWindow).__mdtLastLoads?.(4) ?? [];
			return (rows[0] as { labels?: Record<string, string> }).labels ?? {};
		});

		// The contention half always reaches the row; one load alone is solo.
		expect(labels.solo).toBe('1');
		expect(labels.concurrent_loads).toBe('1');

		// The machine half is present only when the engine could measure, and
		// an age without a reading beside it would be a row claiming one.
		if (pressure.available === true) {
			const readings = ['load_avg_1m', 'mem_free_mb', 'swap_used_mb'].filter(
				(name) => labels[name] !== undefined
			);
			expect(readings.length).toBeGreaterThan(0);
			expect(Number(labels.pressure_age_ms)).toBeGreaterThanOrEqual(0);
			for (const name of readings) expect(Number.isFinite(Number(labels[name]))).toBe(true);
		} else {
			expect(labels.pressure_age_ms).toBeUndefined();
		}
	} finally {
		await context.close();
	}
});
