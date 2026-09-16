/**
 * CUEOUT-15 R5: Space stops a playing preview first, then is released.
 *
 * Drives real keyboard events through the real page, so the capture-phase
 * listener has to actually beat `performance-hotkeys.ts`'s deck Space toggle.
 * `tests/unit/preview-space-stop.test.mjs` pins the decision logic.
 *
 * Regression lines:
 * - if Space while a preview sounds also toggles the playing deck then stopping a preview in the booth stops the room - broken
 * - if Space does not stop the preview then the operator has no quick way to silence it - broken
 * - if the next Space after that does not reach the deck then transport is dead until reload - broken
 * - if Space typed into a text field stops the preview then searching kills the preview - broken
 */
import { expect, test, type Page } from '@playwright/test';

import type { PerformanceCommand, PerformanceState } from '../../src/lib/rb/performance-ipc.svelte';

const TRACK_ROW = '[data-testid="track-row"]';

async function _query(page: Page): Promise<PerformanceState> {
	return page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});
}

async function _dispatch(page: Page, command: PerformanceCommand): Promise<void> {
	await page.evaluate(async (cmd) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch(cmd);
	}, command);
}

async function _stableIdOfRow(page: Page, index: number): Promise<string> {
	const id = await page.locator(TRACK_ROW).nth(index).getAttribute('data-stable-id');
	if (id === null) throw new Error(`row ${index} has no data-stable-id`);
	return id;
}

async function _startPreview(page: Page, stable_id: string): Promise<void> {
	await _dispatch(page, { type: 'preview_cue', stable_id, ratio: 0.3 });
	await expect
		.poll(async () => (await _query(page)).preview.playing, { timeout: 30_000 })
		.toBe(true);
}

test('Space stops a preview without touching the playing deck, then works as normal', async ({
	page
}) => {
	// ?muted=1 mutes after the silence watchdog's tap, so the deck keeps
	// playing without the room hearing it.
	await page.goto('/performance?muted=1');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect.poll(async () => page.locator(TRACK_ROW).count()).toBeGreaterThan(1);
	// Page chrome, not a focused control.
	await page.locator('body').click({ position: { x: 2, y: 2 } });

	const onAir = await _stableIdOfRow(page, 1);
	await _dispatch(page, { type: 'load', deck: 1, stable_id: onAir });
	await expect
		.poll(async () => (await _query(page)).decks[1].duration_ms, { timeout: 45_000 })
		.not.toBeNull();
	await _dispatch(page, { type: 'play', deck: 1, playing: true });
	await expect.poll(async () => (await _query(page)).decks[1].playing).toBe(true);

	await _startPreview(page, await _stableIdOfRow(page, 0));

	await page.keyboard.press('Space');
	await expect
		.poll(
			async () => (await _query(page)).preview.stable_id,
			{ message: 'if Space does not stop the preview then the operator has no quick way to silence it - broken' }
		)
		.toBeNull();
	// Give a leaked toggle time to land before asserting it did not.
	await page.waitForTimeout(500);
	expect(
		(await _query(page)).decks[1].playing,
		'if Space while a preview sounds also toggles the playing deck then stopping a preview in the booth stops the room - broken'
	).toBe(true);

	await page.keyboard.press('Space');
	await expect
		.poll(async () => (await _query(page)).decks[1].playing, {
			message: 'if the next Space after that does not reach the deck then transport is dead until reload - broken'
		})
		.toBe(false);

	// A text field keeps its space, and the preview keeps playing.
	await _startPreview(page, await _stableIdOfRow(page, 0));
	await page.evaluate(() => {
		const input = document.createElement('input');
		input.type = 'text';
		input.id = 'space-stop-probe';
		document.body.appendChild(input);
		input.focus();
	});
	await page.keyboard.press('Space');
	await page.waitForTimeout(300);
	expect(
		(await _query(page)).preview.playing,
		'if Space typed into a text field stops the preview then searching kills the preview - broken'
	).toBe(true);
	await expect(page.locator('#space-stop-probe')).toHaveValue(' ');

	await _dispatch(page, { type: 'preview_stop' });
	await _dispatch(page, { type: 'unload', deck: 1 });
});
