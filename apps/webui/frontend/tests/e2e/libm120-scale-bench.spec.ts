/**
 * LIBM-120's production-browser measurements on a synthetic scale fixture.
 *
 * The config builds a fresh deterministic 10k or 20k fixture, serves the
 * committed production build from the real engine, and this test records the
 * engine's deck-load telemetry plus real browser LoAF and Event Timing data.
 * It is a measurement lane, not a threshold gate, and its output is a load
 * floor: the fixture manifest says `release_evidence: false`, so these numbers
 * are never LIBM-120 feature or release evidence.
 *
 * - [if] the production build is absent [then] the config fails before serving.
 * - [if] the engine's own build-info is not a clean repo checkout, or the served build's
 *   `_app/version.json` is not that commit [then] the run fails, never records (PR #4547 review).
 * - [if] the page sends any request to an origin other than the fixture engine serving it,
 *   sends an API call under a base path (a bundle built with VITE_API_BASE), or sends no
 *   API call at all [then] the run fails, never records (PR #4547 review).
 * - [if] the spec runs outside playwright.libm120.config.ts [then] it fails, never skips.
 * - [if] the fixture manifest does not match the configured size and seed [then] the run fails.
 * - [if] deck load does not commit telemetry [then] the run fails, never estimates.
 * - [if] a measured click does not register as a browser interaction [then] the run fails.
 * - [if] the measured All Tracks click does not paint and complete an All Tracks
 *   listing of every fixture row inside the window [then] the run fails, never records.
 * - A fresh context boots onto All Tracks, where a click is a no-op, so the bench
 *   first opens the fixture playlist and waits for it to load fully, then
 *   measures the switch back to All Tracks (PR #4547 review).
 * - [if] the platform is Windows [then] the test skips, naming the POSIX-only launcher.
 * - [if] the browser lacks a requested observer [then] the result says unknown.
 * - LoAF and INP count only entries that start after the deck loads finish.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import {
	LIBM120_MANIFEST,
	LIBM120_PLATFORM_SUPPORTED,
	LIBM120_POSIX_ONLY_REASON,
	LIBM120_PROJECT,
	LIBM120_SEED,
	LIBM120_TRACKS as TRACKS
} from './libm120-settings';

const OUTPUT_PATH = resolve(process.env.LIBM120_OUT ?? `test-results/libm120-${TRACKS}.json`);
const SAMPLES = 5;
/** Chromium never reports an Event Timing entry shorter than this (spec minimum). */
const EVENT_TIMING_MIN_MS = 16;
/** Progressive playlist/index fills are sequential; 10k+ rows can take many minutes before `library-load-*` lands. */
const NEW_PERF_ROW_TIMEOUT_MS = TRACKS >= 10_000 ? 1_800_000 : 60_000;
/** The generator's one playlist (scripts/perf/generate_library_scale_fixture.py). */
const FIXTURE_PLAYLIST_NAME = `LIBM-120 synthetic ${TRACKS}`;

type FixtureManifest = {
	audio_sha256: string;
	generator_revision: number;
	release_evidence: false;
	seed: number;
	synthetic: true;
	tracks_requested: number;
};
type DeckSample = { total_ms: number; stable_id: string };
type BrowserSample = {
	all_tracks_switch_first_rows_ms: number;
	loaf_p75_ms: number | null;
	loaf_frames: number | null;
	inp_p75_ms: number | null;
	interactions_counted: number | null;
	interactions_reported: number | null;
	inp_note: string | null;
};
type BenchResult = {
	tracks: number;
	fixture: {
		seed: number;
		synthetic: true;
		audio_seconds: number;
		audio_sha256: string;
		generator_revision: number;
		release_evidence: false;
	};
	build: { checkout_sha: string; frontend_build_version: string; frontend: 'static-build' };
	deck_load_ms: { samples: DeckSample[]; p50_ms: number };
	all_tracks_switch_first_rows_ms: number;
	loaf_p75_ms: number | null;
	loaf_frames: number | null;
	inp_p75_ms: number | null;
	interactions_counted: number | null;
	interactions_reported: number | null;
	inp_note: string | null;
	measurement: string;
};
type PerfRingRow = {
	kind: string;
	stages?: Record<string, number>;
	labels?: Record<string, string>;
};
type PerfRingWindow = Window & { __mdtPerfLog?: () => readonly PerfRingRow[] };
type ObserverState = {
	loaf: Array<{ start: number; duration: number }>;
	events: Array<{ start: number; duration: number; id: number }>;
	observer_support: string[];
};

function readManifest(): FixtureManifest {
	const manifest = JSON.parse(readFileSync(LIBM120_MANIFEST, 'utf8')) as FixtureManifest;
	if (
		manifest.tracks_requested !== TRACKS ||
		manifest.seed !== LIBM120_SEED ||
		manifest.synthetic !== true ||
		manifest.release_evidence !== false ||
		typeof manifest.audio_sha256 !== 'string'
	) {
		throw new Error(`fixture manifest does not match this run: ${JSON.stringify(manifest)}`);
	}
	return manifest;
}

/**
 * The commit this run measures, in the engine's own words: `/api/v1/build-info`
 * resolves git once at engine start, from the checkout the engine, generator and
 * harness all run from. Anything but a clean repo checkout is refused, never
 * recorded, and the spec launches no git of its own.
 */
async function engineCheckoutSha(request: APIRequestContext): Promise<string> {
	const response = await request.get('/api/v1/build-info');
	if (!response.ok()) throw new Error(`engine build-info failed: HTTP ${response.status()}`);
	const info = (await response.json()) as {
		source?: unknown;
		git_sha_full?: unknown;
		git_dirty?: unknown;
	};
	if (info.source !== 'repo' || typeof info.git_sha_full !== 'string') {
		throw new Error(
			`LIBM-120 needs an engine running from a repo checkout: ${JSON.stringify(info)}`
		);
	}
	if (info.git_dirty !== false) {
		throw new Error(
			`LIBM-120 refuses a dirty checkout at ${info.git_sha_full}; commit or drop the changes`
		);
	}
	return info.git_sha_full;
}

/**
 * The served bundle's own identity (svelte.config.js stamps `kit.version.name`
 * with the build's sha, plus `-dirty` for an uncommitted tree), so a stale or
 * foreign build/ cannot be measured as this commit.
 */
async function verifyServedBuild(request: APIRequestContext, checkoutSha: string): Promise<string> {
	const response = await request.get('/_app/version.json');
	if (!response.ok())
		throw new Error(`served build has no _app/version.json: HTTP ${response.status()}`);
	let payload: { version?: unknown };
	try {
		payload = (await response.json()) as { version?: unknown };
	} catch {
		throw new Error('served _app/version.json is not JSON, so no build manifest is being served');
	}
	if (payload.version === `${checkoutSha}-dirty`) {
		throw new Error(
			`build/ was made from a dirty tree at ${checkoutSha}; rebuild from a clean checkout`
		);
	}
	if (payload.version !== checkoutSha) {
		throw new Error(
			`build/ is ${String(payload.version)}, not this checkout's ${checkoutSha}; run pnpm build here`
		);
	}
	return checkoutSha;
}

function median(values: number[]): number {
	if (values.length === 0) throw new Error('cannot compute a median from no samples');
	const sorted = [...values].sort((left, right) => left - right);
	return sorted[Math.floor(sorted.length / 2)];
}

async function firstPlayableStableId(request: APIRequestContext): Promise<string> {
	// Relative to the config's baseURL, so it always reaches the engine the config started.
	const response = await request.get('/api/v1/tracks?limit=1&available=true');
	if (!response.ok()) throw new Error(`track listing failed: HTTP ${response.status()}`);
	const payload = (await response.json()) as {
		items?: Array<{ stable_id?: string; file_exists?: boolean }>;
	};
	const stableId = payload.items?.find((item) => item.file_exists)?.stable_id;
	if (stableId === undefined) throw new Error('fixture listing had no playable track');
	return stableId;
}

async function loadDeck(page: Page, stableId: string): Promise<DeckSample> {
	const previousGeneration = await page.evaluate(
		() => window.musicDjToolsPerformance?.query().decks[1].load_generation ?? 0
	);
	await page.evaluate(async (id) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC unavailable');
		await ipc.dispatch({ type: 'load', deck: 1, stable_id: id });
	}, stableId);
	await page.waitForFunction(
		(previous) => {
			const deck = window.musicDjToolsPerformance?.query().decks[1];
			return (
				deck !== undefined && deck.load_generation > previous && deck.last_load_stages !== null
			);
		},
		previousGeneration,
		{ timeout: 45_000 }
	);
	const deck = await page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1]);
	const total = deck.last_load_stages?.total;
	if (typeof total !== 'number' || !Number.isFinite(total)) {
		throw new Error('deck-load telemetry has no finite total stage');
	}
	return { total_ms: total, stable_id: stableId };
}

/** Every perf ring row now present, by identity, so a later read can tell new rows apart. */
async function perfRingSnapshot(page: Page): Promise<string[]> {
	return page.evaluate(() => {
		const read = (window as PerfRingWindow).__mdtPerfLog;
		if (read === undefined) throw new Error('perf ring __mdtPerfLog unavailable');
		return read().map((row) => JSON.stringify(row));
	});
}

/**
 * Wait for a perf ring row absent from `before` and return its stages. The
 * rows come from the app's own BrowserPanel telemetry (library-perf.ts), so a
 * new row proves the production listing path ran, not that a click landed.
 */
async function waitForNewPerfRow(
	page: Page,
	before: string[],
	kind: string,
	labels: Record<string, string>,
	what: string,
	timeoutMs: number = NEW_PERF_ROW_TIMEOUT_MS
): Promise<Record<string, number>> {
	const handle = await page
		.waitForFunction(
			({ before, kind, labels }) => {
				const rows = (window as PerfRingWindow).__mdtPerfLog?.() ?? [];
				for (const row of rows) {
					if (row.kind !== kind || before.includes(JSON.stringify(row))) continue;
					if (Object.entries(labels).some(([key, value]) => row.labels?.[key] !== value)) continue;
					return row.stages ?? {};
				}
				return null;
			},
			{ before, kind, labels },
			{ timeout: timeoutMs }
		)
		.catch((error: unknown) => {
			throw new Error(
				`${what}: no new ${kind} perf row within ${Math.round(timeoutMs / 1000)} s (${String(error)})`
			);
		});
	return (await handle.jsonValue()) as Record<string, number>;
}

/**
 * Leave All Tracks for the fixture playlist and wait until its load completes.
 *
 * A fresh context boots onto All Tracks, so clicking All Tracks there returns
 * early in `_selectPlaylistFromCommand` and does no listing or render work.
 * This makes the measured click a real transition. It runs before the window
 * opens, and waits for the whole fill, so none of its frames enter the samples.
 */
async function settleOnFixturePlaylist(page: Page): Promise<void> {
	const before = await perfRingSnapshot(page);
	await page.getByTestId('playlist-row').filter({ hasText: FIXTURE_PLAYLIST_NAME }).click();
	const loaded = await waitForNewPerfRow(
		page,
		before,
		'library-load-playlist',
		{},
		`opening ${FIXTURE_PLAYLIST_NAME} before the window`
	);
	if (loaded.rows !== TRACKS) {
		throw new Error(`${FIXTURE_PLAYLIST_NAME} loaded ${loaded.rows} rows, expected ${TRACKS}`);
	}
	await expect(page.getByTestId('playlist-all-tracks')).not.toHaveClass(/\bselected\b/);
}

async function interactionCount(page: Page): Promise<number | null> {
	return page.evaluate(() =>
		'interactionCount' in performance
			? (performance as { interactionCount: number }).interactionCount
			: null
	);
}

async function measureBrowserResponsiveness(page: Page): Promise<BrowserSample> {
	await settleOnFixturePlaylist(page);
	// The window opens here, after every deck load and the fixture playlist's
	// fill, so boot, decode and deck replacement frames never enter the samples.
	const windowStart = await page.evaluate(() => performance.now());
	const countBefore = await interactionCount(page);
	// Taken after windowStart: a row absent from it was written inside the window.
	const ringBefore = await perfRingSnapshot(page);
	// A Playwright locator click is a trusted input; HTMLElement.click() is not,
	// and an untrusted click never gets an interactionId, so INP would omit it.
	await page.getByTestId('playlist-all-tracks').click();
	// Proof the window held real listing work: the All Tracks pane painted and
	// then completed with every fixture row, both stamped by the production
	// BrowserPanel path. A no-op click writes neither row, so the run fails here.
	const firstRows = await waitForNewPerfRow(
		page,
		ringBefore,
		'library-switch-first-rows',
		{ source: 'all-tracks' },
		'measured All Tracks click'
	);
	const completed = await waitForNewPerfRow(
		page,
		ringBefore,
		'library-load-all-tracks',
		{},
		'measured All Tracks click'
	);
	if (completed.rows !== TRACKS) {
		throw new Error(
			`All Tracks completed with ${completed.rows} rows inside the window, expected ${TRACKS}`
		);
	}
	const switchFirstRowsMs = firstRows.first_rows_ms;
	if (typeof switchFirstRowsMs !== 'number' || !Number.isFinite(switchFirstRowsMs)) {
		throw new Error('All Tracks switch row has no finite first_rows_ms stage');
	}
	await expect(page.getByTestId('playlist-all-tracks')).toHaveClass(/\bselected\b/);
	await expect(page.getByTestId('track-row').first()).toBeVisible({ timeout: 30_000 });
	const countAfterAllTracks = await interactionCount(page);
	if (countBefore !== null && countAfterAllTracks !== null && countAfterAllTracks <= countBefore) {
		throw new Error('All Tracks click registered no browser interaction; INP would not include it');
	}
	await page.getByTestId('track-row').first().click();
	await page.waitForTimeout(250);
	const countAfter = await interactionCount(page);
	const counted = countBefore === null || countAfter === null ? null : countAfter - countBefore;
	const state = await page.evaluate(
		() => (window as Window & { __libm120?: ObserverState }).__libm120 ?? null
	);
	if (state === null) throw new Error('browser observer state missing');
	const p75 = (values: number[]): number | null =>
		values.length === 0
			? null
			: [...values].sort((a, b) => a - b)[Math.ceil(values.length * 0.75) - 1];

	let loafP75: number | null = null;
	let loafFrames: number | null = null;
	if (state.observer_support.includes('long-animation-frame')) {
		const frames = state.loaf
			.filter((entry) => entry.start >= windowStart)
			.map((entry) => entry.duration);
		loafFrames = frames.length;
		loafP75 = p75(frames);
	}

	let inpP75: number | null = null;
	let reported: number | null = null;
	let note: string | null = null;
	if (!state.observer_support.includes('event') || counted === null) {
		note = 'Event Timing or performance.interactionCount unavailable; INP unknown';
	} else {
		const byInteraction = new Map<number, number>();
		for (const entry of state.events) {
			if (entry.start < windowStart) continue;
			byInteraction.set(entry.id, Math.max(byInteraction.get(entry.id) ?? 0, entry.duration));
		}
		const durations = [...byInteraction.values()].sort((a, b) => a - b);
		reported = durations.length;
		// Interactions the browser counted but did not report were under the
		// Event Timing minimum, so they rank below every reported duration.
		const unreported = Math.max(0, counted - durations.length);
		const rank = Math.ceil(counted * 0.75) - 1;
		if (rank < unreported) {
			note = `p75 interaction was under the ${EVENT_TIMING_MIN_MS} ms Event Timing reporting minimum`;
		} else {
			inpP75 = durations[rank - unreported];
		}
	}
	return {
		all_tracks_switch_first_rows_ms: switchFirstRowsMs,
		loaf_p75_ms: loafP75,
		loaf_frames: loafFrames,
		inp_p75_ms: inpP75,
		interactions_counted: counted,
		interactions_reported: reported,
		inp_note: note
	};
}

test('records LIBM-120 production deck load and responsiveness', async ({ page, request }) => {
	test.skip(!LIBM120_PLATFORM_SUPPORTED, LIBM120_POSIX_ONLY_REASON);
	const project = test.info().project.name;
	if (project !== LIBM120_PROJECT) {
		throw new Error(
			`LIBM-120 runs only through playwright.libm120.config.ts (project ${LIBM120_PROJECT}), got ${project}`
		);
	}
	// Large sequential fills (20k playlist + All Tracks) need headroom; stay under globalTimeout.
	test.setTimeout(3_300_000);
	const manifest = readManifest();
	const checkoutSha = await engineCheckoutSha(request);
	const frontendBuildVersion = await verifyServedBuild(request, checkoutSha);
	await page.addInitScript(() => {
		const state: ObserverState = { loaf: [], events: [], observer_support: [] };
		(window as Window & { __libm120?: ObserverState }).__libm120 = state;
		for (const type of ['long-animation-frame', 'event'] as const) {
			try {
				new PerformanceObserver((list) => {
					for (const entry of list.getEntries()) {
						if (type === 'long-animation-frame') {
							state.loaf.push({ start: entry.startTime, duration: entry.duration });
							continue;
						}
						const id = (entry as PerformanceEntry & { interactionId?: number }).interactionId ?? 0;
						if (id === 0) continue;
						state.events.push({ start: entry.startTime, duration: entry.duration, id });
					}
				}).observe({ type, buffered: true, ...(type === 'event' ? { durationThreshold: 0 } : {}) });
				state.observer_support.push(type);
			} catch {
				// Unsupported observer is recorded as null, never estimated.
			}
		}
	});
	// Every request the page makes must reach the engine serving it, never a
	// VITE_API_BASE daemon baked into the build, with or without a base path: record
	// every http(s) origin, every API call at the root, and every one under a prefix.
	const requestOrigins = new Set<string>();
	const prefixedApiCalls = new Set<string>();
	let rootApiCalls = 0;
	page.on('request', (sent) => {
		const url = new URL(sent.url());
		if (url.protocol !== 'http:' && url.protocol !== 'https:') return;
		requestOrigins.add(url.origin);
		if (url.pathname.startsWith('/api/')) rootApiCalls += 1;
		else if (url.pathname.includes('/api/')) prefixedApiCalls.add(`${url.origin}${url.pathname}`);
	});
	const stableId = await firstPlayableStableId(request);
	await page.goto('/performance', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 30_000
	});
	await page.waitForFunction(
		() => typeof (window as PerfRingWindow).__mdtPerfLog === 'function',
		undefined,
		{ timeout: 30_000 }
	);
	const warmup = await loadDeck(page, stableId);
	const samples: DeckSample[] = [];
	for (let index = 0; index < SAMPLES; index += 1) samples.push(await loadDeck(page, stableId));
	const browser = await measureBrowserResponsiveness(page);
	const pageOrigin = new URL(page.url()).origin;
	const foreign = [...requestOrigins].filter((origin) => origin !== pageOrigin);
	if (foreign.length > 0) {
		throw new Error(
			`the page called ${foreign.join(', ')}, not only the fixture engine at ${pageOrigin}; rebuild without VITE_API_BASE`
		);
	}
	if (prefixedApiCalls.size > 0) {
		throw new Error(
			`the bundle sent API calls under a base path (${[...prefixedApiCalls].slice(0, 3).join(', ')}); rebuild without VITE_API_BASE`
		);
	}
	if (rootApiCalls === 0)
		throw new Error('the page sent no API request, so its API origin is unproven');
	const result: BenchResult = {
		tracks: TRACKS,
		fixture: {
			seed: manifest.seed,
			synthetic: true,
			audio_seconds: 30,
			audio_sha256: manifest.audio_sha256,
			generator_revision: manifest.generator_revision,
			release_evidence: false
		},
		build: {
			checkout_sha: checkoutSha,
			frontend_build_version: frontendBuildVersion,
			frontend: 'static-build'
		},
		deck_load_ms: { samples, p50_ms: median(samples.map((sample) => sample.total_ms)) },
		...browser,
		measurement:
			`Production Playwright Chromium; warmup excluded (${warmup.total_ms.toFixed(1)} ms); ` +
			`${SAMPLES} deck-load samples; fixture playlist opened and fully loaded before the window; ` +
			'trusted clicks on All Tracks (a real switch, proven by its first-rows and completion ' +
			'perf rows inside the window) and the first track row; ' +
			'LoAF and INP windowed to entries starting after the deck loads; INP is the p75 of ' +
			'per-interaction maximum Event Timing durations; null with a note means unknown. ' +
			'Synthetic load floor, not LIBM-120 release evidence.'
	};
	mkdirSync(dirname(OUTPUT_PATH), { recursive: true });
	writeFileSync(OUTPUT_PATH, `${JSON.stringify(result, null, 2)}\n`, 'utf8');
	console.log(`[OK] LIBM-120 ${TRACKS}-track measurement ${OUTPUT_PATH}`);
});
