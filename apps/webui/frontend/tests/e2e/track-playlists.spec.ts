/**
 * LIBM-29: Show in playlists track menu navigates the tree to a live playlist.
 */
import { expect, test } from '@playwright/test';

const PLAYLIST_ROW = '[data-testid="playlist-row"]';
const FIXTURE_PLAYLIST_NAME = 'E2E Fixture Set';

test('Show in playlists lists seeded fixture membership and navigates on click', async ({
	page
}) => {
	test.setTimeout(90_000);

	let stableId = '';

	await test.step('boot performance with fixture playlist in tree', async () => {
		await page.goto('/performance');
		const firstRow = page.locator('[data-testid="track-row"]').first();
		await expect(firstRow).toBeVisible({ timeout: 30_000 });
		stableId = (await firstRow.getAttribute('data-stable-id')) ?? '';
		expect(stableId).toBeTruthy();

		await expect(
			page.locator(PLAYLIST_ROW).filter({ hasText: FIXTURE_PLAYLIST_NAME })
		).toBeVisible({ timeout: 30_000 });
	});

	await test.step('popover lists fixture playlist membership', async () => {
		const targetRow = page.locator(`[data-testid="track-row"][data-stable-id="${stableId}"]`);
		await targetRow.click({ button: 'right' });
		await page.getByRole('menuitem', { name: 'Show in playlists' }).click();

		const popover = page.locator('[data-testid="track-playlists-menu"]');
		await expect(popover).toBeVisible();
		await expect(popover).toContainText(FIXTURE_PLAYLIST_NAME);
	});

	await test.step('navigation selects playlist in tree', async () => {
		const popover = page.locator('[data-testid="track-playlists-menu"]');
		await popover.getByRole('menuitem', { name: FIXTURE_PLAYLIST_NAME }).click();
		const selectedRow = page.locator(`${PLAYLIST_ROW}.selected`, {
			hasText: FIXTURE_PLAYLIST_NAME
		});
		await expect(selectedRow).toBeVisible({ timeout: 15_000 });
	});
});
