import { expect, test } from '@playwright/test';

test('add to playlist picker POSTs items:add', async ({ page }) => {
	const addRequests: string[] = [];
	page.on('request', (request) => {
		if (request.url().includes('/items:add')) {
			addRequests.push(request.url());
		}
	});

	await page.goto('/performance');
	await page.waitForSelector('[data-testid="track-row"]', { timeout: 60_000 });
	await page.waitForSelector('[data-testid="playlist-tree"]', { timeout: 60_000 });

	const trackRow = page.locator('[data-testid="track-row"]').first();
	await trackRow.click({ button: 'right' });
	await page.getByRole('menuitem', { name: /Add to playlist/i }).click();

	const playlistButton = page
		.locator('[data-testid="add-to-playlist-picker"] button')
		.filter({ hasNotText: /search/i })
		.first();
	await playlistButton.click();

	await expect(page.getByText(/Added 1 track to/i)).toBeVisible({ timeout: 10_000 });
	expect(addRequests.length).toBeGreaterThan(0);
	expect(addRequests.some((url) => url.includes('/items:add'))).toBe(true);
});
