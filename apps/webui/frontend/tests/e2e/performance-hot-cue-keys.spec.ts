/**
 * #10354: digit keys 1-8 trigger, Shift saves, Cmd+Shift clears hot cues on the
 * recent deck via the performance shortcut resolver (fixture dev server).
 */
import { expect, test, type Page } from '@playwright/test';

import {
	dispatch,
	gotoPerformance,
	ownGridTrack,
	query,
	waitForGrid
} from './support/beat-sync-e2e';

const TRACK_TITLE = 'webkit-fixture-a-128bpm';
const HOT_CUE_SLOT = 'A';
const FIXTURE_MODE = process.env.PERFORMANCE_E2E_FIXTURE !== '0';

async function slotA(page: Page) {
	const slots = (await query(page)).decks[1].hot_cue_slots;
	return slots.find((s) => s.slot === HOT_CUE_SLOT);
}

test.use({
	launchOptions: { args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio'] }
});

test('digit keys save, trigger and clear hot cue A on deck 1', async ({ page, request }) => {
	test.skip(!FIXTURE_MODE, 'requires PERFORMANCE_E2E_FIXTURE !== 0');

	const track = await ownGridTrack(request, TRACK_TITLE);
	await gotoPerformance(page);
	await dispatch(page, { type: 'load', deck: 1, stable_id: track.stable_id });
	await waitForGrid(page, 1);

	const existing = await slotA(page);
	if (existing?.cue !== null && existing?.cue !== undefined) {
		await page.keyboard.press('ControlOrMeta+Shift+Digit1');
		await expect
			.poll(async () => (await slotA(page))?.cue, { message: 'clear fixture slot A' })
			.toBeNull();
	}

	await dispatch(page, { type: 'seek', deck: 1, position_ms: 10_000 });
	await expect
		.poll(async () => (await query(page)).decks[1].position_ms, { message: 'seek to 10s' })
		.toBeGreaterThan(5000);

	await page.keyboard.press('Shift+Digit1');

	let cueInMs: number;
	await expect
		.poll(
			async () => {
				const cue = (await slotA(page))?.cue;
				if (cue === null || cue === undefined) return null;
				cueInMs = cue.in_ms;
				return cue;
			},
			{ message: 'Shift+1 saves hot cue A' }
		)
		.not.toBeNull();

	const beforeOverwrite = cueInMs!;
	await page.keyboard.press('Shift+Digit1');
	await expect
		.poll(async () => (await slotA(page))?.cue?.in_ms, {
			message: 'filled slot must not be overwritten by Shift+1'
		})
		.toBe(beforeOverwrite);

	await dispatch(page, { type: 'seek', deck: 1, position_ms: 60_000 });
	await expect
		.poll(async () => Math.abs((await query(page)).decks[1].position_ms - beforeOverwrite), {
			message: 'seek away from cue'
		})
		.toBeGreaterThan(1000);

	await page.keyboard.press('Digit1');
	await expect
		.poll(async () => Math.abs((await query(page)).decks[1].position_ms - beforeOverwrite), {
			message: 'Digit1 seeks to saved cue'
		})
		.toBeLessThanOrEqual(50);

	await page.keyboard.press('ControlOrMeta+Shift+Digit1');
	await expect
		.poll(async () => (await slotA(page))?.cue, { message: 'Cmd+Shift+1 clears slot A' })
		.toBeNull();
	await expect(page.getByTestId('hot-cue-1-A')).not.toHaveClass(/\bfilled\b/);
});
