// requirement: PERFMODE-14
// [if] Gig holds four decks then Library mode dwells 60s [then] emit footprint and CPU medians

import { expect, test, type APIRequestContext, type CDPSession } from '@playwright/test';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { kpiCaptureTimeoutS } from './kpi-capture-timeouts.mjs';
import { fixtureManifestExists, readFixtureManifest } from './support/fixture-manifest';
import { sampleRendererProcessFootprint } from './support/renderer-process-sample';

const RESULT_PATH = process.env.KPI_CAPTURE_RESULT;
const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const DWELL_SECONDS = Number(process.env.KPI_CAPTURE_DWELL_SECONDS ?? '60');
const SAMPLE_INTERVAL_MS = Number(process.env.KPI_CAPTURE_SAMPLE_INTERVAL_S ?? '5') * 1000;
const FIXTURE_MODE = process.env.PERFORMANCE_E2E_FIXTURE !== '0';
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const DATA_DIR = process.env.MDT_DATA_DIR ?? join(REPOSITORY_ROOT, 'data');
const MANIFEST_PATH = join(DATA_DIR, 'fixture-manifest.json');
const SAMPLE_INTERVAL_S = Number(process.env.KPI_CAPTURE_SAMPLE_INTERVAL_S ?? '5');
const SAMPLING_METHOD =
	`CDP SystemInfo.getProcessInfo selects Chrome renderer + gpu-process PIDs; ps -o rss=,%cpu= for footprint and CPU every ${SAMPLE_INTERVAL_S}s`;

interface ModeCapture {
	footprint_samples_mb: number[];
	cpu_samples_percent: number[];
	median_footprint_mb: number;
	median_cpu_percent: number;
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

async function dwellSample(
	cdp: CDPSession,
	dwellSeconds: number,
	intervalMs: number
): Promise<ModeCapture> {
	const footprintSamples: number[] = [];
	const cpuSamples: number[] = [];
	const endAt = Date.now() + dwellSeconds * 1000;
	while (Date.now() < endAt) {
		const sample = await sampleRendererProcessFootprint(cdp);
		if (sample.footprint_mb !== null) footprintSamples.push(sample.footprint_mb);
		if (sample.cpu_percent !== null) cpuSamples.push(sample.cpu_percent);
		await new Promise((resolve) => setTimeout(resolve, intervalMs));
	}
	if (footprintSamples.length === 0 || cpuSamples.length === 0) {
		throw new Error(
			`insufficient telemetry during dwell (footprint=${footprintSamples.length}, cpu=${cpuSamples.length})`
		);
	}
	return {
		footprint_samples_mb: footprintSamples,
		cpu_samples_percent: cpuSamples,
		median_footprint_mb: median(footprintSamples),
		median_cpu_percent: median(cpuSamples)
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
	const response = await request.get(`${apiBase}/api/v1/tracks?limit=50&available=true`);
	expect(response.ok(), 'available track listing must succeed for library-mode capture').toBeTruthy();
	const payload = (await response.json()) as {
		items: { stable_id: string; file_exists: boolean }[];
	};
	const ids = payload.items.filter((track) => track.file_exists).map((track) => track.stable_id);
	if (ids.length < 4) {
		throw new Error(`need 4 playable tracks, found ${ids.length}`);
	}
	return ids.slice(0, 4);
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

test('captures Gig vs Library steady-state footprint and CPU medians', async ({ page, request }) => {
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

		const cdp = await page.context().newCDPSession(page);
		const gig = await dwellSample(cdp, DWELL_SECONDS, SAMPLE_INTERVAL_MS);
		await selectLibraryMode(page);
		await page.waitForFunction(() => window.__mdtLibraryModeIdle === true);
		const library = await dwellSample(cdp, DWELL_SECONDS, SAMPLE_INTERVAL_MS);

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
