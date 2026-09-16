/**
 * CUEOUT-15 R6 in a real page: a preview tempo-matches the playing master.
 *
 * The rules themselves are pinned by `tests/unit/preview-beat-sync.test.mjs`.
 * What only a browser can show is the wiring: that the setting is read, that
 * the master deck's live BPM is the one used, and that the rate reaches the
 * source node and is reported back over IPC.
 *
 * The tempos are derived from the master deck's own BPM at runtime rather than
 * hardcoded, so the test says what it means ("5 percent away matches, 3x does
 * not") on whatever fixture library it is pointed at.
 *
 * Regression lines:
 * - if a preview does not take the master tempo then the operator auditions against a clashing pulse - broken
 * - if a tempo three times off is matched then a preview plays at a tempo the track cannot hold - broken
 * - if the setting is off and the preview is still re-pitched then the dropdown does nothing - broken
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

async function _previewRate(page: Page, stable_id: string, bpm: number): Promise<number> {
	await _dispatch(page, { type: 'preview_cue', stable_id, ratio: 0.3, bpm });
	await expect
		.poll(async () => (await _query(page)).preview.playing, { timeout: 30_000 })
		.toBe(true);
	const rate = (await _query(page)).preview.rate;
	await _dispatch(page, { type: 'preview_stop' });
	return rate;
}

/** Open the settings overlay, find the row by its own tooltip, pick an option. */
async function _setPreviewBeatSync(page: Page, value: 'off' | 'tempo'): Promise<void> {
	await page.keyboard.press('Meta+Comma');
	const select = page.getByTitle('Tempo-match a library preview to the playing master deck');
	await expect(select).toBeVisible();
	await select.selectOption(value);
	await page.keyboard.press('Escape');
	await expect(select).toBeHidden();
}

test('a preview takes the master deck tempo when the match fits, and never when it does not', async ({
	page
}) => {
	// ?muted=1 mutes after the silence watchdog's tap, so the master deck can
	// really play without the room hearing it.
	await page.goto('/performance?muted=1');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect.poll(async () => page.locator(TRACK_ROW).count()).toBeGreaterThan(1);

	const onAir = await _stableIdOfRow(page, 1);
	await _dispatch(page, { type: 'load', deck: 1, stable_id: onAir });
	await expect
		.poll(async () => (await _query(page)).decks[1].duration_ms, { timeout: 45_000 })
		.not.toBeNull();
	await _dispatch(page, { type: 'master', deck: 1 });
	await _dispatch(page, { type: 'play', deck: 1, playing: true });
	await expect
		.poll(async () => {
			const deck = (await _query(page)).decks[1];
			return deck.playing && deck.is_master && deck.effective_bpm !== null;
		})
		.toBe(true);

	const masterBpm = (await _query(page)).decks[1].effective_bpm;
	if (masterBpm === null) throw new Error('master deck reported no effective BPM');
	const previewed = await _stableIdOfRow(page, 0);

	// A track 5 percent slower than the master: inside the range, so it matches.
	const inRange = await _previewRate(page, previewed, masterBpm / 1.05);
	expect(
		inRange,
		'if a preview does not take the master tempo then the operator auditions against a clashing pulse - broken'
	).toBeCloseTo(1.05, 3);

	// Three times off is outside the range and is not a half or double fold.
	const outOfRange = await _previewRate(page, previewed, masterBpm / 3);
	expect(
		outOfRange,
		'if a tempo three times off is matched then a preview plays at a tempo the track cannot hold - broken'
	).toBe(1);

	// The dropdown itself, driven the way the operator drives it.
	await _setPreviewBeatSync(page, 'off');
	const switchedOff = await _previewRate(page, previewed, masterBpm / 1.05);
	expect(
		switchedOff,
		'if the setting is off and the preview is still re-pitched then the dropdown does nothing - broken'
	).toBe(1);

	await _setPreviewBeatSync(page, 'tempo');
	await _dispatch(page, { type: 'play', deck: 1, playing: false });
	await _dispatch(page, { type: 'unload', deck: 1 });
});
