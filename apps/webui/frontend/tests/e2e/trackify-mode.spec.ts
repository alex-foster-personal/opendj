// requirement: PERFMODE-15
// [if] Trackify route is opened from the mode chooser [then] the listening shell renders

import { expect, test } from '@playwright/test';

const MISSING_STABLE_ID = 'trackify-e2e-missing-stable-id';

async function _openTrackifyFromGig(page: import('@playwright/test').Page): Promise<void> {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const picker = page.locator('details.mode-picker');
	await picker.locator('summary[aria-label="Choose app mode"]').click();
	await picker.locator('a.mode-card').filter({ hasText: 'Trackify' }).click();
	await page.waitForURL((url) => url.pathname === '/music-player');
	await expect(page.getByTestId('trackify-player')).toBeVisible();
	await page.waitForFunction(() => window.musicDjToolsTrackify?.version === 1);
}

test('mode chooser navigates to Trackify listening shell', async ({ page }) => {
	await _openTrackifyFromGig(page);
});

test('failed load skips to the next track with a dismissible toast within 2 s', async ({ page }) => {
	test.setTimeout(120_000);
	await _openTrackifyFromGig(page);
	const goodId = await page.evaluate(async () => {
		const response = await fetch('/api/v1/tracks?limit=1');
		if (!response.ok) throw new Error(`tracks list failed (${response.status})`);
		const payload = await response.json();
		const items = Array.isArray(payload.items) ? payload.items : [];
		if (items.length === 0) throw new Error('fixture library has no tracks');
		return items[0].stable_id as string;
	});
	const startedAt = await page.evaluate(
		async ({ missingId, goodId }) => {
			const ipc = window.musicDjToolsTrackify;
			if (ipc?.e2e_prime_feed === undefined || ipc.e2e_force_load === undefined) {
				throw new Error('Trackify e2e hooks are unavailable');
			}
			ipc.toggle_autoplay(false);
			const perf = window.musicDjToolsPerformance;
			if (perf !== undefined && perf.query().decks[1].stable_id !== null) {
				await perf.dispatch({ type: 'unload', deck: 1 });
			}
			ipc.e2e_prime_feed([
				{
					stable_id: missingId,
					key: '8A',
					bpm: 124,
					file_exists: true,
					title: 'Missing',
					artist: 'E2E'
				},
				{
					stable_id: goodId,
					key: '8A',
					bpm: 124,
					file_exists: true,
					title: 'Good',
					artist: 'E2E'
				}
			]);
			const started = performance.now();
			await ipc.e2e_force_load(missingId);
			return started;
		},
		{ missingId: MISSING_STABLE_ID, goodId }
	);
	await expect(page.getByText(/Trackify: skipped track/)).toBeVisible({ timeout: 3_000 });
	const elapsedMs = await page.evaluate((started) => performance.now() - started, startedAt);
	expect(elapsedMs).toBeLessThan(2_000);
	await expect(page.locator('[data-toast-dismiss]').first()).toBeVisible();
	await page.waitForFunction(
		(goodId) => window.musicDjToolsTrackify?.query().deck.stable_id === goodId,
		goodId,
		{ timeout: 10_000 }
	);
});
