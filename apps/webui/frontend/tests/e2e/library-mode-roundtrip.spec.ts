// requirement: PERFMODE-14
// [if] Gig loads decks then Library then Gig [then] decks return empty unless rescue applies
// [if] Gig -> Library -> Gig happens in-app (no reload) [then] a deck loads and plays again, past the
//   silence watchdog's window, at the master volume it had before [⛔️ if the teardown's hard mute
//   leaks into the remounted Gig and the watchdog cuts the deck ~2 s in]

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

/** Back to Gig through the Library layout's own Performance nav link: a
 * client-side navigation, so the document (and the engine singleton) survives. */
async function _returnToGigInApp(page: import('@playwright/test').Page): Promise<void> {
	await page.getByRole('link', { name: 'Performance', exact: true }).click();
	await page.waitForURL((url) => url.pathname === '/performance');
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

test('an in-app Library round trip re-arms the engine: a deck loads and plays again', async ({ page }) => {
	// The two round-trip tests above return to Gig with page.goto, a full
	// reload that builds a fresh engine singleton, so they cannot see a
	// teardown that leaves the SAME singleton unable to re-arm. This one stays
	// in one document: Library disposes the engine, then Gig must rebuild the
	// AudioContext and play. The mute set by ?muted=1 survives dispose on
	// purpose, so this stays silent.
	test.skip(!HAS_MANIFEST, 'fixture manifest required for library round-trip');

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.evaluate(async (stableId) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC missing');
		await ipc.dispatch({ type: 'load', deck: 1, stable_id: stableId });
	}, FIXTURE_STABLE_ID);
	const readMasterVolume = () => page.evaluate(() => window.musicDjToolsPerformance?.query().mixer.master);
	const masterBefore = await readMasterVolume();
	expect(masterBefore, 'the master volume must be readable before the round trip').toBeGreaterThan(0);

	await _selectLibraryMode(page);
	await page.waitForFunction(() => window.__mdtLibraryModeIdle === true);
	const idle = await page.evaluate(() => window.musicDjToolsLibraryMode?.read_idle_probe());
	expect(idle?.audio_context_count).toBe(0);

	const documentMarker = await page.evaluate(() => {
		(window as unknown as { __roundTripMarker?: number }).__roundTripMarker = 1;
		return 1;
	});
	await _returnToGigInApp(page);
	expect(
		await page.evaluate(() => (window as unknown as { __roundTripMarker?: number }).__roundTripMarker),
		'if the marker is gone then the return to Gig reloaded the page and this test proves nothing a goto test does not'
	).toBe(documentMarker);
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const decksAfterReturn = await page.evaluate(() =>
		[1, 2, 3, 4].map((deckId) => window.musicDjToolsPerformance?.query().decks[deckId as 1 | 2 | 3 | 4]?.stable_id)
	);
	expect(decksAfterReturn).toEqual([null, null, null, null]);
	expect(
		await readMasterVolume(),
		'if the master is 0 then the teardown hard mute leaked into the remounted Gig (the in-app return mounts /performance twice)'
	).toBe(masterBefore);

	await page.evaluate(async (stableId) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC missing');
		await ipc.dispatch({ type: 'load', deck: 1, stable_id: stableId });
		await ipc.dispatch({ type: 'play', deck: 1, playing: true });
	}, FIXTURE_STABLE_ID);
	await page.waitForFunction(
		(stableId) => {
			const deck = window.musicDjToolsPerformance?.query().decks[1];
			return deck?.stable_id === stableId && deck.playing === true;
		},
		FIXTURE_STABLE_ID,
		{ timeout: 20_000 }
	);
	const readPosition = () =>
		page.evaluate(() => window.musicDjToolsPerformance?.query().decks[1].position_ms ?? -1);
	const start = await readPosition();
	// Past SILENT_WHILE_PLAYING_MS (2 s) with margin: a deck playing into a
	// muted master advances about 2 s, then the silence watchdog stops it.
	await expect
		.poll(readPosition, {
			message: 'if position stalls near 2 s then the silence watchdog cut a deck that was playing into no signal',
			timeout: 15_000
		})
		.toBeGreaterThan(start + 4_000);
	expect(await page.evaluate(() => window.musicDjToolsPerformance?.query().decks[1].playing)).toBe(true);
	const perfKinds = await page.evaluate(() => {
		const read = (window as Window & { __mdtPerfLog?: () => readonly { kind: string }[] }).__mdtPerfLog;
		if (read === undefined) throw new Error('__mdtPerfLog missing, so the silence check below would read nothing');
		return read().map((row) => row.kind);
	});
	expect(perfKinds, 'control: the rebuilt graph armed its xrun sentinel, so the log is being written').toContain(
		'xrun-sentinel-armed'
	);
	expect(perfKinds).not.toContain('silent-while-playing');
	const context = await page.evaluate(() => window.musicDjToolsLibraryMode?.read_idle_probe());
	expect(context?.audio_context_count).toBe(1);
	expect(context?.deck_nodes_present).toBe(true);
	expect(context?.deck_pcm_bytes, 'the reloaded deck holds its decoded buffer again').toBeGreaterThan(0);
});
