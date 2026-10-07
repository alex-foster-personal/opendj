// requirement: PLAY-27
/**
 * PLAY-27 acceptance, end to end (#5735 / #5738). Real engine, real decoded
 * fixture audio on its own Beat This! grid, a real AudioContext. No mocks.
 *
 * The incident: an AutoPlay handoff under Beat Sync Max loaded a track whose
 * tempo the follower's pitch range could not reach. AutoPlay kept Beat Sync
 * lit (Max), the engine refused to start a follower it could not phase-lock,
 * and the room went silent.
 *
 * [if] the AutoPlay handoff lands a synced follower whose tempo is out of its
 *   pitch range [then] the incoming deck still PLAYS (its playhead advances)
 *   and a red toast names the deck and the reason
 *   [else stop: the handoff aborts into silence].
 *
 * Needs the own beatgrid fixture (MDT_BEATGRID_WEIGHTS, see
 * playwright.performance.config.ts); without it the track helper skips and
 * says why.
 */
import { expect, test, type Page } from '@playwright/test';

import { API_BASE, dispatch, ownGridTrack, query, waitAudible, waitForGrid, waitForIpc } from './support/beat-sync-e2e';

const PLAYLIST_ID = 'e2e-fixture-autoplay-chain';
const OUT_OF_RANGE_TITLE = 'webkit-fixture-b-124bpm';
/** Master pitch for the handoff. Both decks on the 8% range (the default is 16): at 8% range a 128 BPM master plays 138.24 BPM
 * and the 124 BPM follower would need +11.5%: out of range, 10% away. */
const MASTER_RATIO = 1.08;

/** Set an AutoPlay option the way a DJ does, from the TopBar's AutoPlay options. */
async function setAutoPlayOption(page: Page, name: string, checked: boolean): Promise<void> {
	await page.getByRole('button', { name: 'AutoPlay', exact: true }).hover();
	const option = page.getByRole('dialog', { name: 'AutoPlay options' }).getByRole('checkbox', { name });
	await expect(option).toBeVisible();
	if ((await option.isChecked()) !== checked) await option.click();
	await expect(option).toBeChecked({ checked });
}

/** Start from empty decks: a Gig rescue snapshot left by an earlier spec may have restored and resumed some. */
async function emptyAllDecks(page: Page): Promise<void> {
	for (const deck of [1, 2, 3, 4] as const) {
		const state = (await query(page)).decks[deck];
		if (state.playing) await dispatch(page, { type: 'play', deck, playing: false });
		if (state.stable_id !== null) await dispatch(page, { type: 'unload', deck });
	}
}

test('PLAY-27: an AutoPlay handoff under Beat Sync Max to an out-of-range tempo plays unsynced, with a red toast', async ({
	page,
	request
}) => {
	const follower = await ownGridTrack(request, OUT_OF_RANGE_TITLE);
	const listing = await request.get(`${API_BASE}/api/v1/playlists/${PLAYLIST_ID}/tracks?limit=50`);
	expect(listing.ok(), 'autoplay-chain playlist').toBeTruthy();
	const members = ((await listing.json()) as { tracks: { stable_id: string; title: string | null }[] }).tracks;
	const at = members.findIndex((m) => m.stable_id === follower.stable_id);
	expect(at, `${OUT_OF_RANGE_TITLE} must follow a 128 BPM track in ${PLAYLIST_ID}`).toBeGreaterThan(0);
	const masterTitle = members[at - 1].title ?? '';
	expect(masterTitle, 'the track before the follower is the 128 BPM master').toMatch(/128bpm/);
	const master = await ownGridTrack(request, masterTitle);

	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto(`/performance?playlist=${PLAYLIST_ID}`);
	await waitForIpc(page);
	await dispatch(page, { type: 'master_volume', value: 0.1 });
	const bsm = page.locator('button.topbar-slot-bsm');
	if ((await bsm.getAttribute('aria-pressed')) !== 'true') await bsm.click();
	await expect(bsm, 'Beat Sync Max on, as in the incident').toHaveAttribute('aria-pressed', 'true');
	await dispatch(page, { type: 'autoplay', enabled: false, by_user: true });
	await emptyAllDecks(page);
	// Playlist order, so the handoff loads the out-of-range track the way the
	// incident did, rather than whichever compatible track AutoPlay would pick.
	await setAutoPlayOption(page, 'Enforce play order', true);
	await setAutoPlayOption(page, 'Maximize reach (avoid stranding)', false);

	for (const deck of [1, 2] as const) await dispatch(page, { type: 'pitch_range', deck, range: 8 });
	await dispatch(page, { type: 'load', deck: 1, stable_id: master.stable_id });
	await waitForGrid(page, 1);
	await dispatch(page, { type: 'master', deck: 1 });
	await dispatch(page, { type: 'tempo', deck: 1, ratio: MASTER_RATIO });
	await dispatch(page, { type: 'beat_sync', deck: 2, enabled: true });
	const duration = (await query(page)).decks[1].duration_ms;
	expect(duration, 'master duration known after load').not.toBeNull();
	await dispatch(page, { type: 'seek', deck: 1, position_ms: Math.max(0, duration! - 30_000) });
	await dispatch(page, { type: 'play', deck: 1, playing: true });
	await waitAudible(page, 1);

	await dispatch(page, { type: 'autoplay', enabled: true, by_user: true });
	await expect
		.poll(async () => (await query(page)).decks[2].stable_id, {
			message: 'AutoPlay loads the out-of-range track onto deck 2',
			timeout: 60_000
		})
		.toBe(follower.stable_id);
	const loaded = await query(page);
	expect(loaded.decks[2].beat_sync_enabled, 'Beat Sync Max keeps the follower lit').toBe(true);
	expect(loaded.decks[2].pitch_range, 'the follower is still on the 8% range').toBe(8);

	// The incoming deck must PLAY: audible, and its playhead advancing.
	await expect
		.poll(async () => (await query(page)).decks[2].audible, {
			message: 'deck 2 audible after the handoff (before the fix: never, the room went silent)',
			timeout: 60_000
		})
		.toBe(true);
	const p0 = (await query(page)).decks[2].position_ms;
	await page.waitForTimeout(2_000);
	const later = await query(page);
	expect(later.decks[2].playing, 'deck 2 still playing').toBe(true);
	expect(later.decks[2].position_ms - p0, 'deck 2 playhead advances').toBeGreaterThan(1_000);
	expect(later.decks[2].pitch, 'an unlocked follower keeps its own tempo').toBeCloseTo(1, 6);

	const red = page.locator('.toast-stack .toast.error', {
		hasText: 'Beat Sync cannot lock deck 2: tempo 10% away; playing unsynced'
	});
	await expect(red, 'a red toast names the deck and the reason').toBeVisible();
});
