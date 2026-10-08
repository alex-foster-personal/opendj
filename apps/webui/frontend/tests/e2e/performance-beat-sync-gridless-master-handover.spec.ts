// requirement: PLAY-28
/**
 * PLAY-28 acceptance, end to end. Real engine, real decoded fixture audio, a
 * real AudioContext. No mocks.
 *
 * The incident (Wed 7 Oct 2026 16:11:29Z, silver, packaged build 2411a8f23,
 * Beat Sync Max on): AutoPlay started the incoming track on deck 1, which had
 * no beat grid, then asked for master with the exact command below. The
 * engine tried to re-lock the outgoing synced deck to a gridless master,
 * refused the whole handover ("deck 1 master handover refused: Beat Sync:
 * deck 1 requires a valid real PQTZ beat grid"), and AutoPlay, which only
 * arms off the master, never queued another track: silence.
 *
 * [if] a synced deck plays and master moves to a playing GRIDLESS deck [then]
 *   the master moves, both decks keep playing, and a warn toast says the mix
 *   is without Beat Sync [else stop: the master handover is refused].
 * [if] master moves to a playing GRIDDED deck instead [then] Beat Sync is run
 *   against it (the outgoing deck re-locks, or the planner refuses by name) and no
 *   "mixed without Beat Sync" notice appears [else stop: the fix over-reaches
 *   and ignores a valid grid].
 *
 * The gridded tracks need the own beatgrid fixture (MDT_BEATGRID_WEIGHTS, see
 * playwright.performance.config.ts); without it the track helper skips and
 * says why. The gridless track is one of the browse-only rescue rows, which
 * the fixture deliberately never analyzes.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { API_BASE, dispatch, ownGridTrack, query, waitAudible, waitForGrid, waitForIpc } from './support/beat-sync-e2e';

const GRIDDED_128 = 'webkit-fixture-a-128bpm';
const GRIDDED_124 = 'webkit-fixture-b-124bpm';
const GRIDLESS = 'webkit-fixture-e-120bpm-scroll';
const NOTICE = 'Deck 1 has no beat grid: mixed without Beat Sync';

async function stableIdByTitle(request: APIRequestContext, title: string): Promise<string> {
	const listing = await request.get(`${API_BASE}/api/v1/tracks?limit=1000&available=true`);
	expect(listing.ok(), 'track listing').toBeTruthy();
	const items = ((await listing.json()) as { items: { stable_id: string; title: string | null }[] }).items;
	const row = items.find((item) => item.title === title);
	test.skip(row === undefined, `fixture track ${title} is not in this library`);
	return row!.stable_id;
}

async function emptyAllDecks(page: Page): Promise<void> {
	for (const deck of [1, 2, 3, 4] as const) {
		const state = (await query(page)).decks[deck];
		if (state.playing) await dispatch(page, { type: 'play', deck, playing: false });
		if (state.stable_id !== null) await dispatch(page, { type: 'unload', deck });
	}
}

/** Deck 2 plays a gridded 128 BPM track as the synced master; deck 1 is loaded with `incoming` and playing. */
async function setUpHandover(page: Page, outgoing: string, incoming: string, incomingGridded: boolean): Promise<void> {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/performance');
	await waitForIpc(page);
	await dispatch(page, { type: 'master_volume', value: 0.1 });
	await dispatch(page, { type: 'autoplay', enabled: false, by_user: true });
	await emptyAllDecks(page);
	const bsm = page.locator('button.topbar-slot-bsm');
	if ((await bsm.getAttribute('aria-pressed')) !== 'true') await bsm.click();
	await expect(bsm, 'Beat Sync Max on, as in the incident').toHaveAttribute('aria-pressed', 'true');

	await dispatch(page, { type: 'load', deck: 2, stable_id: outgoing });
	await waitForGrid(page, 2);
	await dispatch(page, { type: 'master', deck: 2 });
	await dispatch(page, { type: 'beat_sync', deck: 2, enabled: true });
	await dispatch(page, { type: 'play', deck: 2, playing: true });
	await waitAudible(page, 2);

	await dispatch(page, { type: 'load', deck: 1, stable_id: incoming });
	await expect
		.poll(async () => (await query(page)).decks[1].duration_ms, { message: 'deck 1 loaded' })
		.not.toBeNull();
	if (incomingGridded) {
		await waitForGrid(page, 1);
		// Start the incoming deck at its OWN tempo, so a re-lock after the handover is measurable:
		// synced to deck 2 it would already play at 128 and deck 2 would have nothing to move.
		await dispatch(page, { type: 'beat_sync', deck: 1, enabled: false });
	} else {
		// Presence of the precondition, not absence of a failure: the deck really is gridless.
		expect((await query(page)).decks[1].beatgrid_ms.length, `${GRIDLESS} must load with no beat grid`).toBe(0);
	}
	await dispatch(page, { type: 'play', deck: 1, playing: true });
	await waitAudible(page, 1);
	const before = await query(page);
	expect(before.master_deck, 'deck 2 is still master before the handover').toBe(2);
	expect(before.decks[2].beat_sync_enabled, 'deck 2 is synced, so it becomes a follower').toBe(true);
}

test('PLAY-28: master handover to a playing gridless deck stands, both decks keep playing, with a notice', async ({
	page,
	request
}) => {
	const outgoing = await ownGridTrack(request, GRIDDED_128);
	const gridless = await stableIdByTitle(request, GRIDLESS);
	await setUpHandover(page, outgoing.stable_id, gridless, false);

	// The exact command AutoPlay's _promoteMaster sends. Before the fix this rejected.
	await dispatch(page, { type: 'master', deck: 1 });

	const after = await query(page);
	expect(after.master_deck, 'the gridless deck is master').toBe(1);
	const p1 = after.decks[1].position_ms;
	const p2 = after.decks[2].position_ms;
	await page.waitForTimeout(2_000);
	const later = await query(page);
	expect(later.decks[1].playing && later.decks[1].audible, 'deck 1 still playing').toBe(true);
	expect(later.decks[2].playing && later.decks[2].audible, 'deck 2 still playing').toBe(true);
	expect(later.decks[1].position_ms - p1, 'deck 1 playhead advances').toBeGreaterThan(1_000);
	expect(later.decks[2].position_ms - p2, 'deck 2 playhead advances').toBeGreaterThan(1_000);
	expect(later.decks[2].beat_sync_enabled, 'Beat Sync stays lit (Max is never switched off)').toBe(true);
	await expect(page.locator('.toast-stack .toast', { hasText: NOTICE }), 'the notice is visible').toBeVisible();
	await expect(page.locator('.toast-stack .toast', { hasText: 'master handover refused' })).toHaveCount(0);
});

test('PLAY-28 control: master handover to a gridded deck still runs Beat Sync against it', async ({ page, request }) => {
	const outgoing = await ownGridTrack(request, GRIDDED_128);
	const incoming = await ownGridTrack(request, GRIDDED_124);
	await setUpHandover(page, outgoing.stable_id, incoming.stable_id, true);
	{
		const before = await query(page);
		expect(before.decks[2].pitch, 'deck 2 plays at its own tempo before the handover').toBeCloseTo(1, 3);
		expect(before.decks[1].pitch, 'deck 1 plays at its own 124 BPM, unsynced').toBeCloseTo(1, 3);
	}

	await dispatch(page, { type: 'master', deck: 1 });

	expect((await query(page)).master_deck, 'deck 1 is master').toBe(1);
	// The sync path ran against the gridded master: deck 2 either re-locked (its tempo moved
	// toward 124/128) or the BAR planner refused this anchor and PLAY-27 said so by name.
	// Measured on the Air, Wed 7 Oct 2026: this fixture's 60 s clips often refuse with
	// "bar sync refuses extrapolated downbeat at the sync anchor", which is still the planner
	// being asked. What an over-correction looks like instead: no sync attempt at all and the
	// gridless notice, which is what the last assertion catches.
	const planner = page.locator('.toast-stack .toast', { hasText: 'Beat Sync cannot lock deck 2' });
	await expect
		.poll(async () => (await query(page)).decks[2].pitch < 0.99 || (await planner.count()) > 0, {
			message: 'deck 2 re-locked to the gridded master, or the planner refused it by name'
		})
		.toBe(true);
	await expect(page.locator('.toast-stack .toast', { hasText: 'mixed without Beat Sync' })).toHaveCount(0);
});
