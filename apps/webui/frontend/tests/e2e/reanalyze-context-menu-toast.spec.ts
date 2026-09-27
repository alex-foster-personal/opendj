import { expect, test } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

/**
 * Re-analyze context menu shows queue outcome in the collapsed toast headline.
 *
 * - [if] enqueue admits zero tracks [then] headline states nothing queued [else stop].
 */

const MEMORY_MODEL = {
	backend: 'own_beatgrid.backfill',
	producer_version: '1',
	floor_mb: 1,
	slope_mb_per_min: 1,
	measured_on: 'test',
	source: 'test'
};

test('Re-analyze shows zero-admitted status in collapsed toast headline', async ({ page }) => {
	await page.route('**/api/v1/analysis/backfill/enqueue', async (route) => {
		await route.fulfill({
			json: {
				batch_id: 'qb_e2e_zero',
				offered: 100,
				admitted: 0,
				refused: 100,
				workers: 0,
				band: 'empty',
				memory_model: MEMORY_MODEL
			}
		});
	});
	await page.route('**/api/v1/analysis/backfill/progress?*', async (route) => {
		await route.fulfill({
			json: {
				batch_id: 'qb_e2e_zero',
				state: 'done',
				workers: 0,
				band: 'empty',
				memory_model: MEMORY_MODEL,
				counts: {
					pending: 0,
					running: 0,
					done: 0,
					skipped: 0,
					failed: 0,
					refused: 100,
					cancelled: 0
				},
				total: 100,
				settled: 100,
				created_at: 't',
				updated_at: 't',
				items: [
					{
						stable_id: 'fixture-track',
						lane: 'beatgrid',
						backend: 'own_beatgrid.backfill',
						state: 'refused',
						reason: 'already fresh',
						attempts: 0,
						duration_s: null,
						predicted_peak_mb: null
					}
				]
			}
		});
	});

	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);

	const track = page.locator('[data-testid="track-row"]').first();
	await track.click({ button: 'right' });
	await page.getByRole('menuitem', { name: 'Re-analyze' }).click();

	const headline = page.locator('.toast-headline').first();
	await expect(headline).toBeVisible({ timeout: 10_000 });
	await expect(headline).toContainText(/nothing queued/i);
	await expect(headline).toContainText(/queued 0 of 100/i);
	await expect(headline).toContainText(/beatgrid/i);
});

test('Re-analyze shows running status in collapsed toast headline', async ({ page }) => {
	let progressCalls = 0;
	await page.route('**/api/v1/analysis/backfill/enqueue', async (route) => {
		await route.fulfill({
			json: {
				batch_id: 'qb_e2e_running',
				offered: 2,
				admitted: 2,
				refused: 0,
				workers: 1,
				band: 'under_20_min',
				memory_model: MEMORY_MODEL
			}
		});
	});
	await page.route('**/api/v1/analysis/backfill/progress?*', async (route) => {
		progressCalls += 1;
		const state = progressCalls < 3 ? 'running' : 'done';
		const counts =
			state === 'running' ?
				{
					pending: 0,
					running: 1,
					done: 1,
					skipped: 0,
					failed: 0,
					refused: 0,
					cancelled: 0
				}
			:	{
					pending: 0,
					running: 0,
					done: 2,
					skipped: 0,
					failed: 0,
					refused: 0,
					cancelled: 0
				};
		await route.fulfill({
			json: {
				batch_id: 'qb_e2e_running',
				state,
				workers: 1,
				band: 'under_20_min',
				memory_model: MEMORY_MODEL,
				counts,
				total: 2,
				settled: state === 'done' ? 2 : 1,
				created_at: 't',
				updated_at: 't',
				items: []
			}
		});
	});

	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);

	const track = page.locator('[data-testid="track-row"]').first();
	await track.click({ button: 'right' });
	await page.getByRole('menuitem', { name: 'Re-analyze' }).click();

	const headline = page.locator('.toast-headline').first();
	await expect(headline).toBeVisible({ timeout: 10_000 });
	await expect(headline).toContainText(/running/i, { timeout: 15_000 });
	await expect(headline).toContainText(/1 active/i);
	await expect(headline).toContainText(/beatgrid/i);
});
