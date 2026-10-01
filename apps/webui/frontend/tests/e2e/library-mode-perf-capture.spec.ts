// requirement: PERFMODE-14
// [if] Gig holds four decks then Library mode dwells 60s [then] emit footprint and CPU medians

import { expect, test, type APIRequestContext, type CDPSession } from '@playwright/test';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { kpiCaptureTimeoutS } from './kpi-capture-timeouts.mjs';
import { fixtureManifestExists, readFixtureManifest } from './support/fixture-manifest';
import { EnginePidMismatchError, sampleProcessFamilyFootprint } from './support/renderer-process-sample';

const RESULT_PATH = process.env.KPI_CAPTURE_RESULT;
const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const DWELL_SECONDS = Number(process.env.KPI_CAPTURE_DWELL_SECONDS ?? '60');
// The requirement reads the state once Library "is active for 60 s after a
// switch", so each mode settles before its dwell instead of averaging in the
// teardown spike (issue #3960: Library ticks fell 1222 -> ~600 MB inside the
// dwell). Both modes get the SAME settle, so neither side is favored.
const SETTLE_SECONDS = Number(process.env.KPI_CAPTURE_SETTLE_SECONDS ?? '60');
const SAMPLE_INTERVAL_MS = Number(process.env.KPI_CAPTURE_SAMPLE_INTERVAL_S ?? '5') * 1000;
const FIXTURE_MODE = process.env.PERFORMANCE_E2E_FIXTURE !== '0';
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const DATA_DIR = process.env.MDT_DATA_DIR ?? join(REPOSITORY_ROOT, 'data');
const MANIFEST_PATH = join(DATA_DIR, 'fixture-manifest.json');
const SAMPLE_INTERVAL_S = Number(process.env.KPI_CAPTURE_SAMPLE_INTERVAL_S ?? '5');
const SAMPLING_METHOD =
	'phys_footprint (footprint -p) and ps %cpu of this capture\'s own process family, attributed by pid tree: ' +
	'every Chromium process CDP SystemInfo.getProcessInfo (browser target) lists, plus the engine listening on ' +
	`the API port and every descendant (stem workers); settle ${SETTLE_SECONDS}s per mode, then a sample every ${SAMPLE_INTERVAL_S}s. ` +
	'RSS of the same pids is recorded alongside for continuity.';
const MIN_SAMPLE_FRACTION = 0.5;
// Codex P1/BLOCKING (PR #4034, discussion_r4138153190): MIN_SAMPLE_FRACTION
// alone is relative to expectedTicks, which collapses to 1 when
// KPI_CAPTURE_SAMPLE_INTERVAL_S is set to the dwell length or longer -- a
// single reading then satisfies ceil(1 * 0.5) = 1 and could still flip
// LIB-MODE/PERFMODE-14 to PASS without establishing steady-state behavior.
// This is an ABSOLUTE floor on top of the relative one (mirrors
// _MIN_SCORED_SAMPLES in scripts/perf/capture_library_mode.py, which also
// refuses to score a row below it, independently, on the Python side).
const MIN_ABSOLUTE_SAMPLES = 6;
const MAX_LISTING_PAGES = 40;
const DECK_LOAD_TIMEOUT_MS = 120_000;

interface ModeCapture {
	footprint_samples_mb: number[];
	cpu_samples_percent: number[];
	rss_samples_mb: number[];
	median_footprint_mb: number;
	median_cpu_percent: number;
	median_rss_mb: number;
	median_chromium_footprint_mb: number;
	median_engine_footprint_mb: number;
	engine_pid_counts: number[];
	/** The last sample's Chromium footprint per process type (attribution only). */
	last_chromium_by_type_mb: Record<string, number>;
	/** Count of ticks that threw (and so were excluded from every array
	 * above), even though enough OTHER ticks cleared the minimum-sample
	 * floor. Sol P1/BLOCKING (PR #4034, discussion_r4148668268): failures
	 * were accumulated in a local `failures` array only to report if the
	 * floor was MISSED; once the floor was cleared they were silently
	 * dropped from the row entirely. A failure mode that is more likely in
	 * one Gig/Library phase than the other (e.g. a transient CDP hiccup
	 * during the heavier phase) would then selectively thin that phase's
	 * denominator without leaving any trace on a still-`measured: true`
	 * row. The Python consumer (`_rows_from_capture_result`) refuses to
	 * score a row where this is nonzero. */
	sample_failure_count: number;
}

interface CaptureResult {
	ok: boolean;
	capture_id: string | null;
	gig: ModeCapture | null;
	library: ModeCapture | null;
	stable_ids: string[];
	sampling_method: string;
	reason: string | null;
}

let resultWritten = false;

function writeResult(result: CaptureResult): void {
	if (!RESULT_PATH) {
		throw new Error('KPI_CAPTURE_RESULT is required');
	}
	writeFileSync(RESULT_PATH, JSON.stringify(result), 'utf-8');
	resultWritten = true;
}

function median(values: number[]): number {
	const sorted = [...values].sort((a, b) => a - b);
	const mid = Math.floor(sorted.length / 2);
	if (sorted.length === 0) {
		throw new Error('median requires at least one sample');
	}
	if (sorted.length % 2 === 0) {
		return (sorted[mid - 1] + sorted[mid]) / 2;
	}
	return sorted[mid];
}

/** Mutable so one instance can be shared across the Gig AND Library dwells
 * (Sol P1/BLOCKING, PR #4034, discussion_r4138402621): the engine must stay
 * the SAME process across the whole capture, not just within one mode's
 * window, or a restart during the Gig-to-Library handoff would go
 * undetected. */
interface EnginePin {
	pid: number | undefined;
}

async function dwellSample(
	cdp: CDPSession,
	apiBase: string,
	dwellSeconds: number,
	intervalMs: number,
	enginePin: EnginePin
): Promise<ModeCapture> {
	const footprintSamples: number[] = [];
	const cpuSamples: number[] = [];
	const rssSamples: number[] = [];
	const chromiumSamples: number[] = [];
	const engineSamples: number[] = [];
	const enginePidCounts: number[] = [];
	let lastByType: Record<string, number> = {};
	const failures: string[] = [];
	const endAt = Date.now() + dwellSeconds * 1000;
	const expectedTicks = Math.max(1, Math.floor((dwellSeconds * 1000) / intervalMs));
	while (Date.now() < endAt) {
		// Every read throws on failure (see renderer-process-sample.ts) rather
		// than degrading to null, so a bad tick is skipped here and counted
		// against the minimum-sample-count floor below -- never silently
		// averaged in as a partial or mixed-process-set sample.
		try {
			const sample = await sampleProcessFamilyFootprint(cdp, apiBase, enginePin.pid);
			enginePin.pid ??= sample.engine_pids[0];
			footprintSamples.push(sample.footprint_mb);
			cpuSamples.push(sample.cpu_percent);
			rssSamples.push(sample.rss_mb);
			chromiumSamples.push(sample.chromium_footprint_mb);
			engineSamples.push(sample.engine_footprint_mb);
			enginePidCounts.push(sample.engine_pids.length);
			lastByType = sample.chromium_by_type_mb;
		} catch (error) {
			// An engine-pid mismatch is NOT an ordinary bad tick (Codex
			// P1/BLOCKING, PR #4034, discussion_r4138473507): budgeting it
			// against the sample floor let a restart late in the dwell (after
			// enough good ticks already cleared the floor) pass anyway,
			// mixing two process lifetimes into one measured row. The
			// engine's identity changing must end the dwell outright, not
			// spend one of the floor's tolerated misses.
			if (error instanceof EnginePidMismatchError) throw error;
			failures.push(error instanceof Error ? error.message : String(error));
		}
		await new Promise((resolve) => setTimeout(resolve, intervalMs));
	}
	const minSamples = Math.max(MIN_ABSOLUTE_SAMPLES, Math.ceil(expectedTicks * MIN_SAMPLE_FRACTION));
	if (footprintSamples.length < minSamples || cpuSamples.length < minSamples) {
		throw new Error(
			`insufficient telemetry during dwell: got footprint=${footprintSamples.length} ` +
				`cpu=${cpuSamples.length}, need >=${minSamples} of ${expectedTicks} expected ticks. ` +
				`Sample failures: ${failures.slice(0, 3).join(' | ') || 'none'}`
		);
	}
	return {
		footprint_samples_mb: footprintSamples,
		cpu_samples_percent: cpuSamples,
		rss_samples_mb: rssSamples,
		median_footprint_mb: median(footprintSamples),
		median_cpu_percent: median(cpuSamples),
		median_rss_mb: median(rssSamples),
		median_chromium_footprint_mb: median(chromiumSamples),
		median_engine_footprint_mb: median(engineSamples),
		engine_pid_counts: enginePidCounts,
		last_chromium_by_type_mb: lastByType,
		sample_failure_count: failures.length
	};
}

async function resolveDeckStableIds(request: APIRequestContext, apiBase: string): Promise<string[]> {
	if (FIXTURE_MODE && fixtureManifestExists(MANIFEST_PATH)) {
		const manifest = readFixtureManifest(MANIFEST_PATH);
		if (manifest.tracks.length === 0) {
			throw new Error(`fixture manifest at ${MANIFEST_PATH} has zero tracks`);
		}
		const ids = manifest.tracks.map((track) => track.stable_id);
		while (ids.length < 4) {
			ids.push(ids[ids.length % manifest.tracks.length]);
		}
		return ids.slice(0, 4);
	}
	// A real library's `file_exists` is not proof a deck can load the row: on
	// silver it listed Air-side paths the audio route then answered 404, and the
	// flag flapped between requests. So page through the listing and keep only
	// rows whose audio route actually serves bytes (HEAD 200).
	const ids: string[] = [];
	const rejected: string[] = [];
	let cursor: string | null = null;
	for (let page = 0; page < MAX_LISTING_PAGES && ids.length < 4; page += 1) {
		const query = cursor === null ? '' : `&cursor=${encodeURIComponent(cursor)}`;
		const response = await request.get(`${apiBase}/api/v1/tracks?limit=50&available=true${query}`);
		expect(response.ok(), 'available track listing must succeed for library-mode capture').toBeTruthy();
		const payload = (await response.json()) as {
			items: { stable_id: string; file_exists: boolean }[];
			next_cursor: string | null;
		};
		for (const track of payload.items) {
			if (ids.length >= 4) break;
			if (!track.file_exists) continue;
			const audio = await request.head(`${apiBase}/api/v1/tracks/${track.stable_id}/audio`);
			if (audio.status() === 200) {
				ids.push(track.stable_id);
			} else {
				rejected.push(`${track.stable_id.slice(0, 8)}=${audio.status()}`);
			}
		}
		cursor = payload.next_cursor;
		if (cursor === null) break;
	}
	if (ids.length < 4) {
		throw new Error(
			`need 4 tracks whose audio route serves bytes, found ${ids.length} ` +
				`(rejected: ${rejected.join(', ') || 'none'})`
		);
	}
	return ids;
}

async function waitForFourDecksLoaded(
	page: import('@playwright/test').Page,
	stableIds: string[]
): Promise<void> {
	// `dispatch({type:'load'})` resolving is not a loaded deck: a failed load
	// leaves the deck empty and the Gig baseline would be a three-deck (or
	// zero-deck) session reported as four. Require every deck to carry its
	// track with a completed load before the Gig dwell starts.
	await page.waitForFunction(
		(expected) => {
			const ipc = window.musicDjToolsPerformance;
			if (ipc === undefined) return false;
			const decks = ipc.query().decks;
			return expected.every((sid, index) => {
				const deck = decks[(index + 1) as 1 | 2 | 3 | 4];
				return (
					deck !== undefined &&
					deck.stable_id === sid &&
					deck.last_load_latency_ms !== null &&
					(deck.duration_ms ?? 0) > 0
				);
			});
		},
		stableIds,
		{ timeout: DECK_LOAD_TIMEOUT_MS }
	);
}

async function selectLibraryMode(page: import('@playwright/test').Page): Promise<void> {
	const picker = page.locator('details.mode-picker');
	await picker.locator('summary[aria-label="Choose app mode"]').click();
	const libraryCard = picker
		.locator('a.mode-card[data-testid="mode-card"]')
		.filter({ hasText: 'Library' });
	await libraryCard.click();
	await page.waitForURL((url) => url.pathname === '/');
}

test('captures Gig vs Library steady-state footprint and CPU medians', async ({ browser, page, request }) => {
	const captureBudgetS = kpiCaptureTimeoutS(process.env.KPI_CAPTURE_TIMEOUT_S);
	test.setTimeout(captureBudgetS * 1000 + 30_000);
	const captureId =
		process.env.KPI_CAPTURE_ID ??
		`issue-2700-library-mode-${new Date().toISOString().replace(/[:.]/g, '-')}`;

	try {
		const stableIds = await resolveDeckStableIds(request, API_BASE);
		await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
		await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

		for (const [index, stableId] of stableIds.entries()) {
			const deckId = (index + 1) as 1 | 2 | 3 | 4;
			await page.evaluate(
				async ({ deck, sid }) => {
					const ipc = window.musicDjToolsPerformance;
					if (ipc === undefined) throw new Error('performance IPC missing');
					await ipc.dispatch({ type: 'load', deck, stable_id: sid });
				},
				{ deck: deckId, sid: stableId }
			);
		}
		await waitForFourDecksLoaded(page, stableIds);

		// SystemInfo.getProcessInfo answers only on the BROWSER target; a page
		// session rejects every call, so no Gig or Library sample could land.
		const cdp = await browser.newBrowserCDPSession();
		await page.waitForTimeout(SETTLE_SECONDS * 1000);
		const enginePin: EnginePin = { pid: undefined };
		const gig = await dwellSample(cdp, API_BASE, DWELL_SECONDS, SAMPLE_INTERVAL_MS, enginePin);
		await selectLibraryMode(page);
		await page.waitForFunction(() => window.__mdtLibraryModeIdle === true);
		await page.waitForTimeout(SETTLE_SECONDS * 1000);
		const library = await dwellSample(cdp, API_BASE, DWELL_SECONDS, SAMPLE_INTERVAL_MS, enginePin);

		writeResult({
			ok: true,
			capture_id: captureId,
			gig,
			library,
			stable_ids: stableIds,
			sampling_method: SAMPLING_METHOD,
			reason: null
		});
	} catch (error) {
		writeResult({
			ok: false,
			capture_id: captureId,
			gig: null,
			library: null,
			stable_ids: [],
			sampling_method: SAMPLING_METHOD,
			reason: error instanceof Error ? error.message : String(error)
		});
	}
});

test.afterAll(() => {
	if (!RESULT_PATH) return;
	if (!resultWritten) {
		writeResult({
			ok: false,
			capture_id: null,
			gig: null,
			library: null,
			stable_ids: [],
			sampling_method: SAMPLING_METHOD,
			reason: 'capture test exited without writing KPI_CAPTURE_RESULT'
		});
	}
});
