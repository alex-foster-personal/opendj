/**
 * Browser proof for the hot-cue mapping gate (#736), against a real backend
 * and a real (throwaway, unmapped-by-construction) library.
 *
 * djmdCue is keyed by djmdContent.ID, which a locally imported track never
 * has, so hot-cue SAVE for such a deck has nowhere to write and would 404.
 * HotCueBank goes inert-with-tooltip instead of firing that write
 * (PARITY-TODO.md line 134). The unit suites pin the source text and the
 * shared-dispatcher guard; neither proves the pad actually RENDERS dimmed
 * with the tooltip, or that a real click never reaches the network. This
 * spec is that proof.
 *
 * Acceptance:
 *   - [if] the loaded deck reports has_rb_mapping true [then ⛔️] the fixture
 *     stopped being unmapped and every assertion below is void.
 *   - [if] an empty slot on the unmapped deck lacks the inert-mapping class,
 *     the dimmed opacity, or the "cues need a rekordbox mapping" tooltip
 *     [then ⛔️].
 *   - [if] clicking that slot issues any request to
 *     /api/v1/tracks/*\/hot-cues/* [then ⛔️].
 */
import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import type { PerformanceState } from '../../src/lib/rb/performance-ipc.svelte';

const MAPPING_TIP = 'cues need a rekordbox mapping';
const DECK = 1;

async function _query(page: Page): Promise<PerformanceState> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

test('an unmapped deck renders its empty hot-cue pads inert-with-tooltip and fires nothing on click', async ({
	page
}) => {
	// GET /hot-cues (all slots + ETags, api-rb.ts) is a normal read the deck
	// issues on load; only the write verb (PUT .../hot-cues/{slot}, saveHotCue)
	// is what an inert pad must never reach.
	const hotCueWrites: string[] = [];
	page.on('request', (request) => {
		if (request.method() === 'PUT' && /\/api\/v1\/tracks\/[^/]+\/hot-cues\/[A-H]$/.test(request.url())) {
			hotCueWrites.push(`${request.method()} ${request.url()}`);
		}
	});

	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	// Two fixture tracks exist and both carry no rekordbox mapping by
	// construction (support/deckload_fixture.py real-ingests generated
	// audio with no vendor row) -- All Tracks is the whole library here, not
	// the 9k-row production one, so there is no cost to using it.
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
	// A freshly-ingested fixture track carries no existing hot cues, so every
	// slot is empty and every slot on this unmapped deck must be inert.
	await expect(deckPanel.locator('.cue-area .bank .slot:not(.inert-mapping)')).toHaveCount(0);
	const firstPad = pads.first();
	await expect(firstPad).toHaveAttribute('title', MAPPING_TIP);
	const opacity = await firstPad.evaluate((el) => Number(getComputedStyle(el).opacity));
	expect(opacity, 'the inert pad must render visibly dimmed, not merely tagged').toBeLessThan(1);

	const beforeClickBox = await deckPanel.boundingBox();
	await page.screenshot({
		path: 'test-results/hotcue-mapping-gate-before-click.png',
		...(beforeClickBox ? { clip: beforeClickBox } : {})
	});

	await firstPad.click();
	// Give a real click a real chance to reach the network before asserting
	// silence; onSlotClick's inert branch is synchronous, so this is slack,
	// not a race.
	await page.waitForTimeout(500);

	expect(hotCueWrites, 'an inert pad must never reach the hot-cue write path').toEqual([]);
	const afterClick = (await _query(page)).decks[DECK];
	expect(afterClick.command_error, 'an inert click must not surface a command error').toBeNull();

	const afterClickBox = await deckPanel.boundingBox();
	await page.screenshot({
		path: 'test-results/hotcue-mapping-gate-after-click.png',
		...(afterClickBox ? { clip: afterClickBox } : {})
	});
});
