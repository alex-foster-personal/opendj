import { expect, test } from '@playwright/test';

function _isItemRemoval(method: string, url: string): boolean {
	const path = new URL(url).pathname;
	if (method === 'DELETE') return /\/items\/[a-f0-9]+$/.test(path);
	if (method === 'POST') return path.endsWith('/items:remove');
	return false;
}

test('remove from playlist context menu DELETEs by item_id', async ({ page }) => {
	const deleteRequests: string[] = [];
	const putTrackRequests: string[] = [];
	page.on('request', (request) => {
		const url = request.url();
		// LIBM-21 + LIBUX-8626: removal writes by membership item id, one
		// DELETE /items/{item_id} or one bulk POST /items:remove for the selection.
		if (_isItemRemoval(request.method(), url)) {
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

	// Scoped to the tree: the add-to-playlist toast is itself a button (it is
	// click-to-copy) and its message quotes the playlist name, so an unscoped
	// role+name lookup resolves to two elements while the toast is on screen.
	await page.getByTestId('playlist-tree').getByRole('button', { name: playlistName }).click();
	await page.waitForSelector('[data-testid="track-row"]', { timeout: 30_000 });

	deleteRequests.length = 0;
	putTrackRequests.length = 0;
	const memberRow = page.locator('[data-testid="track-row"]').first();
	await memberRow.click({ button: 'right' });
	await page.getByRole('menuitem', { name: /Remove from playlist/i }).click();

	await expect.poll(() => deleteRequests.length, { timeout: 10_000 }).toBeGreaterThan(0);
	expect(deleteRequests.some((url) => /\/items(\/|:remove$)/.test(new URL(url).pathname))).toBe(true);
	expect(putTrackRequests.length).toBe(0);

	// The playlist's ETag moved with every membership write, so read it fresh:
	// the creation ETag answers 412 and the list stayed behind, where the
	// library tabs (LIBUX-8677) reopen it on the next spec's boot, empty.
	// The UI's own remove may still be landing, moving the ETag between the read
	// and the DELETE (409), so retry with a fresh read until the DELETE lands.
	await expect(async () => {
		const etag = (await page.request.get(`/api/v1/playlists/${playlistId}`)).headers()['etag'];
		const removed = await page.request.delete(`/api/v1/playlists/${playlistId}`, {
			headers: { 'If-Match': etag ?? '' }
		});
		expect(removed.ok(), `cleanup DELETE playlist: ${removed.status()}`).toBeTruthy();
	}).toPass({ timeout: 10_000 });
});

test('Delete key removes membership via DELETE not PUT', async ({ page }) => {
	const deleteRequests: string[] = [];
	const putTrackRequests: string[] = [];
	page.on('request', (request) => {
		const url = request.url();
		// LIBM-21 + LIBUX-8626: removal writes by membership item id, one
		// DELETE /items/{item_id} or one bulk POST /items:remove for the selection.
		if (_isItemRemoval(request.method(), url)) {
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

	// Scoped to the tree: the add-to-playlist toast is itself a button (it is
	// click-to-copy) and its message quotes the playlist name, so an unscoped
	// role+name lookup resolves to two elements while the toast is on screen.
	await page.getByTestId('playlist-tree').getByRole('button', { name: playlistName }).click();
	await page.waitForSelector('[data-testid="track-row"]', { timeout: 30_000 });

	deleteRequests.length = 0;
	putTrackRequests.length = 0;
	const memberRow = page.locator('[data-testid="track-row"]').first();
	await memberRow.click();
	await memberRow.press('Backspace');

	await expect.poll(() => deleteRequests.length, { timeout: 10_000 }).toBeGreaterThan(0);
	expect(putTrackRequests.length).toBe(0);

	// The playlist's ETag moved with every membership write, so read it fresh:
	// the creation ETag answers 412 and the list stayed behind, where the
	// library tabs (LIBUX-8677) reopen it on the next spec's boot, empty.
	// The UI's own remove may still be landing, moving the ETag between the read
	// and the DELETE (409), so retry with a fresh read until the DELETE lands.
	await expect(async () => {
		const etag = (await page.request.get(`/api/v1/playlists/${playlistId}`)).headers()['etag'];
		const removed = await page.request.delete(`/api/v1/playlists/${playlistId}`, {
			headers: { 'If-Match': etag ?? '' }
		});
		expect(removed.ok(), `cleanup DELETE playlist: ${removed.status()}`).toBeTruthy();
	}).toPass({ timeout: 10_000 });
});
