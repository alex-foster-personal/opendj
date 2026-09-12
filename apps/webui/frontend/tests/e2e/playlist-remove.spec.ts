import { expect, test } from '@playwright/test';

test('remove from playlist context menu DELETEs by item_id', async ({ page }) => {
	const deleteRequests: string[] = [];
	const putTrackRequests: string[] = [];
	page.on('request', (request) => {
		const url = request.url();
		if (request.method() === 'DELETE' && /\/items\/[a-f0-9]+$/.test(url)) {
			deleteRequests.push(url);
		}
		if (request.method() === 'PUT' && url.includes('/tracks')) {
			putTrackRequests.push(url);
		}
	});

	const playlistName = `LIBM21 remove ${Date.now()}`;
	await page.goto('/performance');
	await page.waitForSelector('[data-testid="track-row"]', { timeout: 60_000 });
	await page.waitForSelector('[data-testid="playlist-tree"]', { timeout: 60_000 });

	const create = await page.request.post('/api/v1/playlists', {
		data: { name: playlistName }
	});
	expect(create.ok()).toBeTruthy();
	const playlistId = (await create.json()).playlist_id;

	const trackRow = page.locator('[data-testid="track-row"]').first();
	await trackRow.click({ button: 'right' });
	await page.getByRole('menuitem', { name: /Add to playlist/i }).click();
	await page
		.locator('[data-testid="add-to-playlist-picker"] button')
		.filter({ hasText: playlistName })
		.click();
	await expect(page.getByText(/Added 1 track to/i)).toBeVisible({ timeout: 10_000 });

	await page.getByRole('button', { name: playlistName }).click();
	await page.waitForSelector('[data-testid="track-row"]', { timeout: 30_000 });

	deleteRequests.length = 0;
	putTrackRequests.length = 0;
	const memberRow = page.locator('[data-testid="track-row"]').first();
	await memberRow.click({ button: 'right' });
	await page.getByRole('menuitem', { name: /Remove from playlist/i }).click();

	await expect.poll(() => deleteRequests.length, { timeout: 10_000 }).toBeGreaterThan(0);
	expect(deleteRequests.some((url) => url.includes(`/items/`))).toBe(true);
	expect(putTrackRequests.length).toBe(0);

	const etag = create.headers()['etag'];
	if (etag) {
		await page.request.delete(`/api/v1/playlists/${playlistId}`, {
			headers: { 'If-Match': etag }
		});
	}
});

test('Delete key removes membership via DELETE not PUT', async ({ page }) => {
	const deleteRequests: string[] = [];
	const putTrackRequests: string[] = [];
	page.on('request', (request) => {
		const url = request.url();
		if (request.method() === 'DELETE' && /\/items\/[a-f0-9]+$/.test(url)) {
			deleteRequests.push(url);
		}
		if (request.method() === 'PUT' && url.includes('/tracks')) {
			putTrackRequests.push(url);
		}
	});

	const playlistName = `LIBM21 key ${Date.now()}`;
	await page.goto('/performance');
	await page.waitForSelector('[data-testid="track-row"]', { timeout: 60_000 });

	const create = await page.request.post('/api/v1/playlists', {
		data: { name: playlistName }
	});
	expect(create.ok()).toBeTruthy();
	const playlistId = (await create.json()).playlist_id;

	const trackRow = page.locator('[data-testid="track-row"]').first();
	await trackRow.click({ button: 'right' });
	await page.getByRole('menuitem', { name: /Add to playlist/i }).click();
	await page
		.locator('[data-testid="add-to-playlist-picker"] button')
		.filter({ hasText: playlistName })
		.click();
	await expect(page.getByText(/Added 1 track to/i)).toBeVisible({ timeout: 10_000 });

	await page.getByRole('button', { name: playlistName }).click();
	await page.waitForSelector('[data-testid="track-row"]', { timeout: 30_000 });

	deleteRequests.length = 0;
	putTrackRequests.length = 0;
	const memberRow = page.locator('[data-testid="track-row"]').first();
	await memberRow.click();
	await memberRow.press('Backspace');

	await expect.poll(() => deleteRequests.length, { timeout: 10_000 }).toBeGreaterThan(0);
	expect(putTrackRequests.length).toBe(0);

	const etag = create.headers()['etag'];
	if (etag) {
		await page.request.delete(`/api/v1/playlists/${playlistId}`, {
			headers: { 'If-Match': etag }
		});
	}
});
