/**
 * CHROME-07 (issue #3886, Codex P2 4129900605 on PR #3896): the bottom-tray
 * Preview button beside the MIDI chip.
 *
 * Its only action is stopping a playing library preview. It used to stay
 * enabled with nothing playing, so it looked operable and a click did nothing.
 * It is now disabled while idle, with a tooltip that says why and how to start
 * a preview, and it still stops a preview that is playing.
 *
 * Root suite (real engine, fixture library, /performance), like
 * io-midi-drawer-handoff.spec.ts. The preview is started through the real
 * `preview_cue` IPC command, the same one preview-cue-library.spec.ts drives.
 *
 * Acceptance tests, "[if] <scenario> [then ⛔️]":
 * - [if] the Preview button is enabled with no preview playing [then ⛔️]
 * - [if] the idle button has no tooltip saying why it is inert [then ⛔️]
 * - control [if] clicking it while a preview plays does not stop it [then ⛔️]
 */
// requirement: CHROME-07
import { expect, test, type Page } from '@playwright/test';

import type { PerformanceCommand, PerformanceState } from '../../src/lib/rb/performance-ipc.svelte';

const IDLE_TITLE = 'No preview playing: click a mini-waveform to start one';

const query = (page: Page): Promise<PerformanceState> =>
	page.evaluate(() => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		return ipc.query();
	});

const dispatch = (page: Page, command: PerformanceCommand): Promise<void> =>
	page.evaluate(async (cmd) => {
		const ipc = window.musicDjToolsPerformance;
		if (ipc === undefined) throw new Error('performance IPC is not installed');
		await ipc.dispatch(cmd);
	}, command);

test('the tray Preview button is inert while idle and stops a playing preview', async ({ page }) => {
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	// Quiet for headed runs; the cue path keeps its own gain.
	await dispatch(page, { type: 'master_volume', value: 0.1 });

	const idle = page.getByRole('button', { name: 'Library preview cue', exact: true });
	await expect(idle).toBeVisible({ timeout: 60_000 });
	expect((await query(page)).preview.playing).toBe(false);
	await expect(idle).toBeDisabled();
	await expect(idle).toHaveAttribute('title', IDLE_TITLE);

	const row = page.locator('[data-testid="track-row"]').first();
	await expect(row).toBeVisible({ timeout: 60_000 });
	const stableId = await row.getAttribute('data-stable-id');
	expect(stableId, 'the fixture row has no data-stable-id').not.toBeNull();
	await dispatch(page, { type: 'preview_cue', stable_id: stableId as string, ratio: 0.3 });
	await expect.poll(async () => (await query(page)).preview.playing, { timeout: 30_000 }).toBe(true);

	const stop = page.getByRole('button', { name: 'Stop library preview', exact: true });
	await expect(stop).toBeEnabled();
	await stop.click();
	await expect.poll(async () => (await query(page)).preview.stable_id).toBeNull();
	expect((await query(page)).preview.playing).toBe(false);
	await expect(idle).toBeDisabled();
});
