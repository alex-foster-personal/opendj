/**
 * Issue #1558: a double-click aimed at the title of an ALREADY-SELECTED
 * library row silently loaded the wrong deck and never played.
 *
 * The quick-load box (`.deck-btns`, TrackTable.svelte) reveals whenever
 * `tr.rb-row-selected:has(.c-art:hover, .c-title:hover)` matches, and before
 * this fix its buttons became clickable the instant that happened
 * (`transition-delay: 0s`). A selected row is exactly what the mouse is
 * already resting on when a double-click begins, so the gesture's SECOND
 * click landed on whichever deck button geometry put under the pointer -
 * "Load onto deck N" fired (no play), and that button's own `ondblclick`
 * calls `stopPropagation()`, so the row's own smart-load-and-play handler
 * (`onRowDblClick`) never ran at all.
 *
 * The fix (TrackTable.svelte, `.deck-btns` CSS) delays only the
 * SELECT-driven reveal's pointer-events past the platform double-click
 * interval (DBLCLICK_GUARD_MS = 500ms), while the box itself keeps
 * `pointer-events: none` throughout (pin fce26c7493b0) - so during the
 * guard window a click at that point passes straight through to the row
 * underneath, exactly as if the box were not there. The corridor-travel
 * (`.deck-btns:hover`) and keyboard-focus (`:focus-within`) reveals are
 * untouched and stay instant.
 *
 * This spec proves the DOWNSTREAM effect through the real audio path (the
 * generated fixture library, real Web Audio decode/worklet), not merely
 * that a CSS rule text changed - `tests/unit/quick-load-deck-box.test.mjs`
 * already pins the source. It runs in the fast per-PR root Playwright suite
 * (real backend + fixture, chromium) rather than the WebKit-only nightly
 * tier, because the defect is browser-agnostic: `hot-cue-mapping-gate.spec.ts`
 * reproduced it under chromium too (see the issue).
 *
 * Acceptance:
 * - [if] a row is selected and its title is then double-clicked [then ⛔️]
 *   the smart-load picker's deck (deck 1 on this empty board) must load
 *   AND play - a silent load-with-no-play is the exact regression.
 * - [if] the pointer rests on the selected row's title for longer than the
 *   double-click interval [then ⛔️] the quick-load box must become
 *   clickable (loading deck 2 through its own button), proving the fix is a
 *   TIMING guard and not a removal of the feature.
 */
import { expect, test, type Page } from '@playwright/test';

import type { PerformanceState } from '../../src/lib/rb/performance-ipc.svelte';

const TRACK_ROW = '[data-testid="track-row"]';
const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

/**
 * Delay the fix guards against, per TrackTable.svelte's DBLCLICK_GUARD_MS
 * comment. A little under it so a double-click gesture this test issues is
 * unambiguously inside the danger window the bug lived in.
 */
const WITHIN_GUARD_MS = 200;
/** Comfortably past the guard, for the "intentional dwell" control. */
const PAST_GUARD_MS = 700;

async function _query(page: Page): Promise<PerformanceState> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

async function _openAllTracks(page: Page): Promise<void> {
	// The app's own persistence, not a stub: the remembered choice it would
	// have written itself, on disk AND in localStorage, so a double-click takes
	// the deterministic no-confirm load-and-play path rather than opening the
	// confirm dialog. Disk is authoritative for confirm keys since PR #4014
	// (hydrateConfirmFromDisk drops a key the disk map lacks), so a
	// localStorage-only seed is reset to "ask" by the boot GET.
	const put = await page.request.put('/api/v1/ui-prefs', {
		data: { confirm: { dblclick_load_play: false } }
	});
	expect(put.ok(), `seed confirm.dblclick_load_play: HTTP ${put.status()}`).toBe(true);
	await page.addInitScript((prefsKey) => {
		window.localStorage.setItem(
			prefsKey,
			JSON.stringify({ hide_broken_links: false, confirm: { dblclick_load_play: false } })
		);
	}, PREFS_STORAGE_KEY);
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1, undefined, {
		timeout: 30_000
	});
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator(TRACK_ROW).first()).toBeVisible({ timeout: 30_000 });
}

async function _waitForDeckLoaded(page: Page, deck: 1 | 2): Promise<void> {
	await page.waitForFunction(
		(deckId) => {
			const state = window.musicDjToolsPerformance?.query().decks[deckId];
			return state !== undefined && state.stable_id !== null && !state.transport_pending;
		},
		deck,
		{ timeout: 45_000 }
	);
}

/** Wait for a deck's PRESENTED `playing` flag, not merely the intent. */
async function _waitForPlaying(page: Page, deck: 1 | 2, expected: boolean): Promise<void> {
	await page.waitForFunction(
		({ deckId, expected }) => {
			const state = window.musicDjToolsPerformance?.query().decks[deckId];
			return (
				state !== undefined &&
				state.playing === expected &&
				!state.transport_pending &&
				state.transport_clock.presented_revision === state.transport_clock.desired_revision
			);
		},
		{ deckId: deck, expected },
		{ timeout: 30_000 }
	);
}

test.describe('quick-load box does not hijack a double-click on a selected row (#1558)', () => {
	// The fixture library is shared by the whole serial root suite, and other
	// specs (library-keyboard-nav) expect the default "ask" dialog.
	test.afterEach(async ({ request }) => {
		const put = await request.put('/api/v1/ui-prefs', {
			data: { confirm: { dblclick_load_play: null } }
		});
		expect(put.ok(), `clear confirm.dblclick_load_play: HTTP ${put.status()}`).toBe(true);
	});

	test('double-clicking a just-selected row loads AND plays the smart-picked deck', async ({
		page
	}) => {
		test.setTimeout(120_000);
		await _openAllTracks(page);
		const row = page.locator(TRACK_ROW).first();
		const stableId = await row.getAttribute('data-stable-id');
		expect(stableId, 'row 0 has no data-stable-id').not.toBeNull();

		// A genuine double-click: Playwright's dblclick fires both clicks back
		// to back, well inside the platform double-click interval - exactly the
		// gesture the bug hijacked. The row is UNSELECTED beforehand, so the
		// first click of this very gesture is what selects it while the pointer
		// is already resting on the title: the reveal's most dangerous instant.
		await row.locator('td.c-title').dblclick();

		await _waitForDeckLoaded(page, 1);
		await _waitForPlaying(page, 1, true);
		const state = await _query(page);
		expect(
			state.decks[1].stable_id,
			'the smart pick on an empty board must be deck 1'
		).toBe(stableId);
		expect(
			state.decks[1].playing,
			'a double-click load must also PLAY - a silent load is the #1558 regression'
		).toBe(true);
		// The regression loaded onto deck 2 instead of the row's own dblclick
		// handler ever running.
		expect(state.decks[2].stable_id).toBeNull();
	});

	test('double-clicking the title of an ALREADY-selected row still loads AND plays', async ({
		page
	}) => {
		test.setTimeout(120_000);
		await _openAllTracks(page);
		const row = page.locator(TRACK_ROW).first();
		const stableId = await row.getAttribute('data-stable-id');
		expect(stableId).not.toBeNull();

		// Select first, as a SEPARATE prior action - not part of the
		// double-click gesture under test - then double-click the same spot
		// almost immediately, mirroring the issue's exact report.
		await row.locator('td.c-title').click();
		await expect(row).toHaveClass(/rb-row-selected/);
		await page.waitForTimeout(WITHIN_GUARD_MS);
		await row.locator('td.c-title').dblclick();

		await _waitForDeckLoaded(page, 1);
		await _waitForPlaying(page, 1, true);
		const state = await _query(page);
		expect(state.decks[1].stable_id, 'the smart pick on an empty board must be deck 1').toBe(
			stableId
		);
		expect(
			state.decks[1].playing,
			'a double-click load on an already-selected row must also PLAY'
		).toBe(true);
		expect(
			state.decks[2].stable_id,
			'the gesture must never land on the quick-load box instead of the row'
		).toBeNull();
	});

	test('dwelling past the guard makes the quick-load box itself usable (a timing guard, not a removal)', async ({
		page
	}) => {
		test.setTimeout(120_000);
		await _openAllTracks(page);
		const row = page.locator(TRACK_ROW).first();
		const stableId = await row.getAttribute('data-stable-id');
		expect(stableId).not.toBeNull();

		await row.locator('td.c-title').click();
		await expect(row).toHaveClass(/rb-row-selected/);
		await row.locator('td.c-title').hover();
		// Rest well past the guard: a deliberate dwell, not a double-click.
		await page.waitForTimeout(PAST_GUARD_MS);
		await expect(row.locator('.deck-btns button').first()).toHaveCSS('pointer-events', 'auto');

		await row.locator('button[title="Load onto deck 2"]').click();
		await _waitForDeckLoaded(page, 2);
		await _waitForPlaying(page, 2, false);
		const state = await _query(page);
		expect(state.decks[2].stable_id).toBe(stableId);
		// A single click on the box's own button loads without play - this is
		// the box's ordinary, unchanged contract.
		expect(state.decks[2].playing).toBe(false);
	});
});
