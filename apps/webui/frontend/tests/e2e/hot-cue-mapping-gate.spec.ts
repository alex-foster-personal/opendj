/**
 * Browser proof that a deck with no rekordbox mapping SAVES hot cues (CUES-01),
 * against a real backend and a real (throwaway, unmapped-by-construction)
 * library. Until CUES-01 cues lived only in rekordbox's djmdCue, so these pads
 * were inert (#736); cues now live in Open DJ's own store in state.db.
 *
 * Acceptance:
 *   - [if] the loaded deck reports has_rb_mapping true [then ⛔️] the fixture
 *     stopped being unmapped and every assertion below is void.
 *   - [if] an empty slot on the unmapped deck is inert (inert-mapping class)
 *     or lacks the save tooltip [then ⛔️].
 *   - [if] naming and committing that slot does not issue a PUT to
 *     /api/v1/tracks/*\/hot-cues/{slot} that returns 200, or the pad does not
 *     render filled afterwards [then ⛔️].
 *
 * A second block below covers #804: a deck with NOTHING loaded defaults
 * `has_rb_mapping` true (`_emptyDeckState`, state.svelte.ts), so it looks
 * mapped rather than unmapped, and its empty pads rendered as normal,
 * live-looking controls that threw `Error: hot cue X: deck is not loaded`
 * (unhandled - no toast, no banner) on click. Needs no track load at all,
 * just a deck at rest.
 *
 * Acceptance:
 *   - [if] a never-loaded deck's empty slots lack the inert-mapping class,
 *     the dimmed opacity, or the "no track loaded" tooltip [then ⛔️].
 *   - [if] clicking a never-loaded deck's empty slot throws a page error, or
 *     issues any request to /api/v1/tracks/*\/hot-cues/* [then ⛔️].
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import type { PerformanceState } from '../../src/lib/rb/performance-ipc.svelte';

const NOT_LOADED_TIP = 'no track loaded - nothing to save';
const DECK = 1;

async function _query(page: Page): Promise<PerformanceState> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

test('an unmapped deck saves a hot cue into the own store (CUES-01)', async ({ page }) => {
	const hotCueWrites: number[] = [];
	page.on('response', (response) => {
		const request = response.request();
		if (request.method() === 'PUT' && /\/api\/v1\/tracks\/[^/]+\/hot-cues\/A$/.test(request.url())) {
			hotCueWrites.push(response.status());
		}
	});

	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	// Two fixture tracks exist and both carry no rekordbox mapping by
	// construction (support/deckload_fixture.py real-ingests generated
	// audio with no vendor row).
	const firstRow = page.locator('.table-wrap table tbody tr[data-stable-id]').first();
	await expect(firstRow).toBeVisible();
	await firstRow.locator('td.c-title').dblclick();
	const confirmYes = page.locator('.load-confirm[role="dialog"] .load-confirm-yes');
	if (await confirmYes.isVisible()) await confirmYes.click();

	await page.waitForFunction(
		() => window.musicDjToolsPerformance?.query().decks[1].stable_id !== null
	);
	const deck = (await _query(page)).decks[DECK];
	expect(deck.command_error, 'deck 1 must load without a command error').toBeNull();
	expect(
		deck.has_rb_mapping,
		'fixture identity: a locally-ingested track must report has_rb_mapping false'
	).toBe(false);

	const deckPanel = page.locator(`section.rb-deck[data-deck="${DECK}"]`);
	const pads = deckPanel.locator('.cue-area .bank .slot');
	await expect(pads).toHaveCount(8);
	await expect(deckPanel.locator('.cue-area .bank .slot.inert-mapping')).toHaveCount(0);
	const firstPad = pads.first();
	await expect(firstPad).toHaveAttribute('title', /click to save the current position/);

	await firstPad.click();
	const nameInput = deckPanel.getByTestId(`hot-cue-name-${DECK}-A`);
	await expect(nameInput).toBeVisible();
	await nameInput.fill('own store');
	await nameInput.press('Enter');

	await expect.poll(() => hotCueWrites, { message: 'SAVE must reach the server and succeed' }).toEqual([200]);
	await expect(firstPad).toHaveClass(/filled/);
	const afterSave = (await _query(page)).decks[DECK];
	expect(afterSave.command_error, 'a save must not surface a command error').toBeNull();

	const box = await deckPanel.boundingBox();
	await page.screenshot({
		path: 'test-results/hotcue-own-store-after-save.png',
		...(box ? { clip: box } : {})
	});
});

test('a never-loaded deck renders its empty hot-cue pads inert-with-tooltip and fires nothing on click (#804)', async ({
	page
}) => {
	const NEVER_LOADED_DECK = 2;

	const hotCueWrites: string[] = [];
	page.on('request', (request) => {
		if (request.method() === 'PUT' && /\/api\/v1\/tracks\/[^/]+\/hot-cues\/[A-H]$/.test(request.url())) {
			hotCueWrites.push(`${request.method()} ${request.url()}`);
		}
	});
	const pageErrors: string[] = [];
	page.on('pageerror', (error) => pageErrors.push(error.message));

	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	// No load on this deck at all - the exact repro from the issue body:
	// goto('/performance'), load nothing, click the deck's first pad.
	const deck = (await _query(page)).decks[NEVER_LOADED_DECK];
	expect(deck.stable_id, 'deck 2 must start with nothing loaded for this repro to be valid').toBeNull();
	expect(
		deck.has_rb_mapping,
		'an empty deck defaults has_rb_mapping true (state.svelte.ts _emptyDeckState) - that is the ' +
			'root cause this test guards: the mapping flag alone cannot tell an empty deck apart'
	).toBe(true);

	const deckPanel = page.locator(`section.rb-deck[data-deck="${NEVER_LOADED_DECK}"]`);
	const pads = deckPanel.locator('.cue-area .bank .slot');
	await expect(pads).toHaveCount(8);
	await expect(deckPanel.locator('.cue-area .bank .slot:not(.inert-mapping)')).toHaveCount(0);
	const firstPad = pads.first();
	await expect(firstPad).toHaveAttribute('title', NOT_LOADED_TIP);
	const opacity = await firstPad.evaluate((el) => Number(getComputedStyle(el).opacity));
	expect(opacity, 'the inert pad must render visibly dimmed, not merely tagged').toBeLessThan(1);

	await firstPad.click();
	// Give a real click a real chance to reach the network / throw before
	// asserting silence; onSlotClick's inert branch is synchronous, so this
	// is slack, not a race.
	await page.waitForTimeout(500);

	expect(hotCueWrites, 'an inert pad must never reach the hot-cue write path').toEqual([]);
	expect(pageErrors, 'clicking an inert empty-deck pad must not throw unhandled (#804)').toEqual([]);
	const afterClick = (await _query(page)).decks[NEVER_LOADED_DECK];
	expect(afterClick.command_error, 'an inert click must not surface a command error').toBeNull();
});
