// requirement: PERFMODE-14
// [if] Gig loads decks then Library then Gig [then] decks return empty unless rescue applies

import { expect, test } from '@playwright/test';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fixtureManifestExists, primaryFixtureStableId } from './support/fixture-manifest';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const DATA_DIR = process.env.MDT_DATA_DIR ?? join(REPOSITORY_ROOT, 'data');
const STATE_DB_PATH = join(DATA_DIR, 'state', 'state.db');
const MANIFEST_PATH = join(DATA_DIR, 'fixture-manifest.json');
const API_BASE = process.env.PERFORMANCE_E2E_API_BASE ?? 'http://127.0.0.1:8686';
const HAS_MANIFEST = fixtureManifestExists(MANIFEST_PATH);
const HAS_STATE_DB = existsSync(STATE_DB_PATH);
const FIXTURE_STABLE_ID = HAS_MANIFEST ? primaryFixtureStableId(MANIFEST_PATH) : '';

async function _selectLibraryMode(page: import('@playwright/test').Page): Promise<void> {
	const picker = page.locator('details.mode-picker');
	await picker.locator('summary[aria-label="Choose app mode"]').click();
	const libraryCard = picker.locator('a.mode-card[data-testid="mode-card"]').filter({ hasText: 'Library' });
	await libraryCard.click();
	await page.waitForURL((url) => url.pathname === '/');
}

function _deckSnapshot(
	deckId: number,
	opts: {
		stable_id: string | null;
		playing: boolean;
		position_ms?: number;
	}
) {
	return {
		deck_id: deckId,
		stable_id: opts.stable_id,
		source_path: null,
		playing: opts.playing,
		position_ms: opts.position_ms ?? 0,
		beat_stamp: { kind: 'sample', position_ms: opts.position_ms ?? 0 },
		pitch: 1,
		pitch_range: 8,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		quantize_enabled: true,
		beat_sync_enabled: true,
		sync_mode: 'bar',
		is_master: false,
		cue_ms: opts.position_ms ?? null,
		loop: null,
		hot_cue_armed: null,
		stems: {
			vocal: { muted: false, solo: false, gain: 0.5 },
			instrumental: { muted: false, solo: false, gain: 0.5 },
			drums: { muted: false, solo: false, gain: 0.5 }
		},
		mixer_channel: {
			trim: 0.5,
			eq_high: 0.5,
			eq_mid: 0.5,
			eq_low: 0.5,
			filter: 0.5,
			fader: 1,
			assign: 'THRU',
			cue_enabled: false
		}
	};
}

async function _seedRescueSnapshot(
	request: import('@playwright/test').APIRequestContext,
	stableId: string
): Promise<void> {
	const nowMs = Date.now();
	const response = await request.post(`${API_BASE}/api/v1/performance/rescue-snapshots`, {
		data: {
			schema: 1,
			captured_at_ms: nowMs - 5 * 60 * 1000,
			reason: 'periodic',
			app_posture: 'gig',
			master_deck: null,
			playlist_id: null,
			deck_layout: 'more',
			decks: {
				1: _deckSnapshot(1, { stable_id: stableId, playing: true, position_ms: 12_000 }),
				2: _deckSnapshot(2, { stable_id: null, playing: false }),
				3: _deckSnapshot(3, { stable_id: null, playing: false }),
				4: _deckSnapshot(4, { stable_id: null, playing: false })
			},
			mixer: {
				crossfader: 0.5,
				master: 0.1,
				headphones: {
					mix: 0.5,
					level: 0.5,
					output_mode: 'practice',
					selected_master_output_device_id: null,
					selected_output_device_id: null
				}
			}
		}
	});
	expect(response.ok()).toBeTruthy();
}

test('library round-trip via PerformanceAppNav returns empty decks', async ({ page }) => {
	test.skip(!HAS_MANIFEST, 'fixture manifest required for library round-trip');

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

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

	await page.getByTestId('performance-nav-library').click();
	await page.waitForURL((url) => url.pathname === '/');
	await page.waitForFunction(() => window.__mdtLibraryModeIdle === true);

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const emptyDecks = await page.evaluate(() => {
		const decks = window.musicDjToolsPerformance?.query().decks;
		return [1, 2, 3, 4].map((deckId) => {
			const deck = decks?.[deckId as 1 | 2 | 3 | 4];
			// `?? 'missing'` would also replace a real `null` stable_id on an empty
			// deck, so the assertion below could never observe an actual null and
			// would always fail wherever the fixture manifest exists (Claude review,
			// round 4, PR #3679). Distinguish "deck object absent" (an error --
			// surfaces loudly as the 'missing' string) from "deck object present
			// with a null stable_id" (the expected empty-deck shape).
			return deck === undefined ? 'missing' : deck.stable_id;
		});
	});
	expect(emptyDecks).toEqual([null, null, null, null]);
});

test('library round-trip returns empty decks', async ({ page }) => {
	test.skip(!HAS_MANIFEST, 'fixture manifest required for library round-trip');

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

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

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const emptyDecks = await page.evaluate(() => {
		const decks = window.musicDjToolsPerformance?.query().decks;
		return [1, 2, 3, 4].map((deckId) => {
			const deck = decks?.[deckId as 1 | 2 | 3 | 4];
			// `?? 'missing'` would also replace a real `null` stable_id on an empty
			// deck, so the assertion below could never observe an actual null and
			// would always fail wherever the fixture manifest exists (Claude review,
			// round 4, PR #3679). Distinguish "deck object absent" (an error --
			// surfaces loudly as the 'missing' string) from "deck object present
			// with a null stable_id" (the expected empty-deck shape).
			return deck === undefined ? 'missing' : deck.stable_id;
		});
	});
	expect(emptyDecks).toEqual([null, null, null, null]);
});

test('RESCUE-02 still restores after Gig -> Library -> Gig', async ({ page, request }) => {
	test.skip(
		!HAS_MANIFEST || !HAS_STATE_DB,
		'fixture manifest and state.db required for rescue auto-restore after library teardown'
	);

	await _seedRescueSnapshot(request, FIXTURE_STABLE_ID);

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

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

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect
		.poll(async () => {
			const stableId = await page.evaluate(
				() => window.musicDjToolsPerformance?.query().decks[1].stable_id ?? null
			);
			return stableId;
		})
		.toBe(FIXTURE_STABLE_ID);
});
