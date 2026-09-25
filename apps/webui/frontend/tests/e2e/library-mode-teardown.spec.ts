// requirement: PERFMODE-14
// [if] Gig loads four decks then Library mode is selected [then] gig audio graph and caches are idle

import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fixtureManifestExists, primaryFixtureStableId } from './support/fixture-manifest';
import { countLiveFamilyMembers } from './support/renderer-process-sample';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const DATA_DIR = process.env.MDT_DATA_DIR ?? join(REPOSITORY_ROOT, 'data');
const MANIFEST_PATH = join(DATA_DIR, 'fixture-manifest.json');
const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const HAS_MANIFEST = fixtureManifestExists(MANIFEST_PATH);
const FIXTURE_STABLE_ID = HAS_MANIFEST ? primaryFixtureStableId(MANIFEST_PATH) : '';

async function _selectLibraryMode(page: import('@playwright/test').Page): Promise<void> {
	const picker = page.locator('details.mode-picker');
	await picker.locator('summary[aria-label="Choose app mode"]').click();
	const libraryCard = picker.locator('a.mode-card[data-testid="mode-card"]').filter({ hasText: 'Library' });
	await libraryCard.click();
	await page.waitForURL((url) => url.pathname === '/');
}

test('library mode teardown clears gig resources', async ({ page, request }) => {
	test.skip(!HAS_MANIFEST, 'fixture manifest required to load decks before library teardown');

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	// Baseline process-family member count BEFORE any deck load, when no stem
	// worker can possibly exist yet. This stands in for a name-based
	// "no stem backend workers" check: the telemetry endpoint never exposes a
	// raw command line, and nothing in apps/stems currently stamps stem
	// workers with an opendj-* identity (set_process_identity), so every stem
	// worker's `name` reads 'unnamed' -- a marker-string search can never
	// match anything the endpoint actually sends (.planning/debt/3679.md,
	// "Stem-worker markers do not match any field the telemetry endpoint
	// exposes"). A live leaked worker is still a live process-family member
	// though, so comparing the LIVE member COUNT before deck loads to the count
	// after Library teardown catches a leak without needing to name it.
	// Probe-log members (processes from the native probe's last record that are
	// not live now) are excluded, so an unrelated exit cannot net out a leak.
	const baselineTelemetry = await _pollTelemetryUntilAvailable(request, API_BASE);
	const baselineMemberCount = countLiveFamilyMembers(baselineTelemetry);

	for (const deck of [1, 2, 3, 4] as const) {
		await page.evaluate(
			async ({ deckId, stableId }) => {
				const ipc = window.musicDjToolsPerformance;
				if (ipc === undefined) throw new Error('performance IPC missing');
				await ipc.dispatch({ type: 'load', deck: deckId, stable_id: stableId });
			},
			{ deckId: deck, stableId: FIXTURE_STABLE_ID }
		);
	}

	await _selectLibraryMode(page);
	await page.waitForFunction(() => window.__mdtLibraryModeIdle === true);

	const librarySnapshot = await page.evaluate(() => {
		const api = window.musicDjToolsLibraryMode;
		if (api === undefined) throw new Error('library mode probe missing');
		return api.read_idle_probe();
	});
	expect(librarySnapshot.audio_context_count).toBe(0);
	expect(librarySnapshot.stem_decoder_pooled_count).toBe(0);
	expect(librarySnapshot.prefetch_ready_count).toBe(0);
	expect(librarySnapshot.anlz_cache_entry_count).toBe(0);
	expect(librarySnapshot.deck_nodes_present).toBe(false);
	expect(librarySnapshot.audio_context_state).toBe('uninitialized');

	const finalTelemetry = await _pollTelemetryUntilAvailable(request, API_BASE);
	const finalMemberCount = countLiveFamilyMembers(finalTelemetry);
	// A stem worker (or anything else) still alive after teardown shows up as
	// an EXTRA process-family member versus the pre-load baseline; a match or
	// a drop is fine (teardown is allowed to free members the baseline held).
	expect(finalMemberCount).toBeLessThanOrEqual(baselineMemberCount);
});

const _TELEMETRY_POLL_ATTEMPTS = 5;
const _TELEMETRY_POLL_INTERVAL_MS = 500;

/** Poll the process telemetry endpoint until it reports `available: true`,
 * bounded, so a transiently-cold cache does not read as "no workers running"
 * (countLiveFamilyMembers throws on it -- unavailable is not evidence of a clean teardown). */
async function _pollTelemetryUntilAvailable(
	request: import('@playwright/test').APIRequestContext,
	apiBase: string
): Promise<Record<string, unknown>> {
	let lastBody: Record<string, unknown> = {};
	for (let attempt = 0; attempt < _TELEMETRY_POLL_ATTEMPTS; attempt++) {
		const response = await request.get(`${apiBase}/api/v1/performance/telemetry/processes`);
		expect(response.ok()).toBeTruthy();
		lastBody = (await response.json()) as Record<string, unknown>;
		if (lastBody.available === true) return lastBody;
		await new Promise((resolve) => setTimeout(resolve, _TELEMETRY_POLL_INTERVAL_MS));
	}
	return lastBody;
}
