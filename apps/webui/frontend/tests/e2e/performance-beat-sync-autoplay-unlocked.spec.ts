// requirement: PLAY-27, PLAY-23 (issue #5662)
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
 * [if] that same handoff runs under Beat Sync Max (decision `keep`) [then]
 *   AutoPlay raises no "Beat Sync skipped ... Beat Sync left off." toast of its
 *   own, the engine's red toast being the only notice (issue #5662)
 *   [else stop: the AutoPlay toast says sync was left off while it is lit].
 * [if] Beat Sync Max is off and the source deck is synced [then] the same
 *   failed plan takes the `disable` branch: the follower ends with Beat Sync
 *   off and AutoPlay's skip toast says so [else stop: the keep assertion above
 *   could pass on a handoff that never reached the failed-plan branch].
 *
 * Toasts are counted from pushToast's own one-line-per-toast console record
 * (`[perf-event] toast-<kind> id=<id>: <message>`, perf-event-log.ts), never
 * from the tray: a toast lives 5 s and the tray keeps three, so absence on
 * screen proves nothing. The red engine toast is read from the same record in
 * the same case, which is what shows the record was being read at all.
 *
 * Needs the own beatgrid fixture (MDT_BEATGRID_WEIGHTS, see
 * playwright.performance.config.ts); without it the track helper skips and
 * says why.
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { API_BASE, dispatch, ownGridTrack, query, waitAudible, waitForGrid, waitForIpc } from './support/beat-sync-e2e';

const PLAYLIST_ID = 'e2e-fixture-autoplay-chain';
const OUT_OF_RANGE_TITLE = 'webkit-fixture-b-124bpm';
/** Master pitch for the handoff. Both decks on the 8% range (the default is 16): at 8% range a 128 BPM master plays 138.24 BPM
 * and the 124 BPM follower would need +11.5%: out of range, 10% away. */
const MASTER_RATIO = 1.08;

/**
 * Set an AutoPlay option the way a DJ does, from the TopBar's AutoPlay options.
 * The pointer leaves the TopBar first: the menu is inside the AutoPlay wrap, so
 * a pointer moving from the menu straight back to the button never re-enters
 * the wrap, and the menu's own pointerleave has already closed it.
 */
async function setAutoPlayOption(page: Page, name: string, checked: boolean): Promise<void> {
	await page.mouse.move(0, 400);
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

/** Every toast line pushToast wrote on this page, in order (one per toast). */
function watchToastLog(page: Page): string[] {
	const lines: string[] = [];
	page.on('console', (message) => {
		const text = message.text();
		if (text.startsWith('[perf-event] toast-')) lines.push(text);
	});
	return lines;
}

/** AutoPlay's own Beat Sync skip notice for deck 2 (formatAutoPlaySyncSkipToast). */
const AUTOPLAY_SKIP_LINE = /^\[perf-event\] toast-info id=\S+: auto-play: deck 2 Beat Sync skipped \(/;
/** The engine's red refusal for deck 2 (PLAY-27). */
const ENGINE_REFUSAL_LINE =
	/^\[perf-event\] toast-error id=\S+: Beat Sync cannot lock deck 2: tempo 10% away; playing unsynced/;

/**
 * Master on deck 1, playing 30 s from its end, with the out-of-range track next
 * in the playlist, and AutoPlay still off. `beatSyncMax` is set through the
 * TopBar button a DJ uses. With Max off the source deck is synced too, so the
 * decision table reaches its failed-plan branch rather than "source Beat Sync
 * off".
 */
async function armHandoffToOutOfRangeTrack(
	page: Page,
	request: APIRequestContext,
	beatSyncMax: boolean
): Promise<string> {
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
	if ((await bsm.getAttribute('aria-pressed')) !== String(beatSyncMax)) await bsm.click();
	await expect(bsm, `Beat Sync Max ${beatSyncMax ? 'on' : 'off'}`).toHaveAttribute(
		'aria-pressed',
		String(beatSyncMax)
	);
	await dispatch(page, { type: 'autoplay', enabled: false, by_user: true });
	await emptyAllDecks(page);
	// Playlist order, so the handoff loads the out-of-range track the way the
	// incident did, rather than whichever compatible track AutoPlay would pick.
	// Maximize reach first: Enforce play order disables its checkbox.
	await setAutoPlayOption(page, 'Enforce play order', false);
	await setAutoPlayOption(page, 'Maximize reach (avoid stranding)', false);
	await setAutoPlayOption(page, 'Enforce play order', true);

	for (const deck of [1, 2] as const) await dispatch(page, { type: 'pitch_range', deck, range: 8 });
	await dispatch(page, { type: 'load', deck: 1, stable_id: master.stable_id });
	await waitForGrid(page, 1);
	await dispatch(page, { type: 'master', deck: 1 });
	await dispatch(page, { type: 'tempo', deck: 1, ratio: MASTER_RATIO });
	if (!beatSyncMax) await dispatch(page, { type: 'beat_sync', deck: 1, enabled: true });
	await dispatch(page, { type: 'beat_sync', deck: 2, enabled: true });
	const armed = await query(page);
	if (!beatSyncMax) expect(armed.decks[1].beat_sync_enabled, 'the source deck is synced').toBe(true);
	const duration = armed.decks[1].duration_ms;
	expect(duration, 'master duration known after load').not.toBeNull();
	await dispatch(page, { type: 'seek', deck: 1, position_ms: Math.max(0, duration! - 30_000) });
	await dispatch(page, { type: 'play', deck: 1, playing: true });
	await waitAudible(page, 1);
	return follower.stable_id;
}

async function expectHandoffLoads(page: Page, followerId: string): Promise<void> {
	await dispatch(page, { type: 'autoplay', enabled: true, by_user: true });
	await expect
		.poll(async () => (await query(page)).decks[2].stable_id, {
			message: 'AutoPlay loads the out-of-range track onto deck 2',
			timeout: 60_000
		})
		.toBe(followerId);
}

// Max off first: the PLAY-27 case after it leaves Max on, as this file always has.
test('#5662 control: with Beat Sync Max off, the same failed plan turns sync off and AutoPlay says so', async ({
	page,
	request
}) => {
	const toastLog = watchToastLog(page);
	const followerId = await armHandoffToOutOfRangeTrack(page, request, false);
	await expectHandoffLoads(page, followerId);

	await expect
		.poll(() => toastLog.filter((line) => AUTOPLAY_SKIP_LINE.test(line)), {
			message: 'AutoPlay raised its skip toast for deck 2 (the disable branch)',
			timeout: 30_000
		})
		.toHaveLength(1);
	const skip = toastLog.find((line) => AUTOPLAY_SKIP_LINE.test(line))!;
	expect(skip, 'the skip toast names the refused plan and the outcome').toMatch(
		/Beat Sync skipped \(bar\): .+\. BAR needs a twin within pitch range \[0\.92, 1\.08\]; Beat Sync left off\.$/
	);
	await expect
		.poll(async () => (await query(page)).decks[2].beat_sync_enabled, {
			message: 'the disable branch turned deck 2 Beat Sync off'
		})
		.toBe(false);
});

test('PLAY-27: an AutoPlay handoff under Beat Sync Max to an out-of-range tempo plays unsynced, with a red toast', async ({
	page,
	request
}) => {
	const toastLog = watchToastLog(page);
	const followerId = await armHandoffToOutOfRangeTrack(page, request, true);
	await expectHandoffLoads(page, followerId);
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
	expect(later.decks[2].beat_sync_enabled, 'Max kept Beat Sync lit through play').toBe(true);

	const red = page.locator('.toast-stack .toast.error', {
		hasText: 'Beat Sync cannot lock deck 2: tempo 10% away; playing unsynced'
	});
	await expect(red, 'a red toast names the deck and the reason').toBeVisible();

	// Issue #5662: under Max the handoff's decision is `keep`, and AutoPlay adds
	// no skip toast of its own. The beat-sync step runs before `play`, so by the
	// time the red refusal (raised by `play`) is in the record, any AutoPlay skip
	// toast would already be in it too. The refusal line is read from the same
	// record, so an unread record cannot pass this as "no skip toast".
	expect(
		toastLog.filter((line) => ENGINE_REFUSAL_LINE.test(line)).length,
		'the toast record holds the engine refusal it was watched for'
	).toBeGreaterThan(0);
	expect(
		toastLog.filter((line) => AUTOPLAY_SKIP_LINE.test(line) || /Beat Sync left off/.test(line)),
		'under Max AutoPlay raises no skip toast (#5662)'
	).toEqual([]);
});
