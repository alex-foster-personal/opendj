/**
 * LIBM-29: Show in playlists track menu navigates the tree to a live playlist.
 */
import { expect, test } from '@playwright/test';

test('Show in playlists lists live memberships and navigates on click', async ({ page }) => {
	test.setTimeout(90_000);
	const stamp = Date.now();
	const nameA = `LIBM-29 A ${stamp}`;
	const nameB = `LIBM-29 B ${stamp}`;
	const createdIds: string[] = [];

	await page.goto('/performance');
	const trackRow = page.locator('[data-testid="track-row"]').first();
	await expect(trackRow).toBeVisible({ timeout: 30_000 });
	const stableId = await trackRow.getAttribute('data-stable-id');
	expect(stableId).toBeTruthy();

	try {
		for (const name of [nameA, nameB]) {
			const created = await page.request.post('/api/v1/playlists', { data: { name } });
			expect(created.ok(), await created.text()).toBeTruthy();
			const body = (await created.json()) as { playlist_id: string };
			createdIds.push(body.playlist_id);
			const etag = created.headers().etag;
			expect(etag).toBeTruthy();
			const replaced = await page.request.put(`/api/v1/playlists/${body.playlist_id}/tracks`, {
				headers: { 'If-Match': etag as string },
				data: { stable_ids: [stableId as string] }
			});
			expect(replaced.ok(), await replaced.text()).toBeTruthy();
		}

		const targetRow = page.locator(`[data-testid="track-row"][data-stable-id="${stableId}"]`);
		await targetRow.click({ button: 'right' });
		await page.getByRole('menuitem', { name: 'Show in playlists' }).click();

		const popover = page.locator('[data-testid="track-playlists-menu"]');
		await expect(popover).toBeVisible();
		await expect(popover).toContainText(nameA);
		await expect(popover).toContainText(nameB);

		await popover.getByRole('menuitem', { name: nameA }).click();
		const selectedRow = page.locator(`[data-testid="playlist-row"].selected`, {
			hasText: nameA
		});
		await expect(selectedRow).toBeVisible({ timeout: 15_000 });
	} finally {
		for (const playlistId of createdIds) {
			const detail = await page.request.get(`/api/v1/playlists/${playlistId}`);
			if (!detail.ok()) continue;
			const etag = detail.headers().etag;
			if (!etag) continue;
			await page.request.delete(`/api/v1/playlists/${playlistId}`, {
				headers: { 'If-Match': etag }
			});
		}
	}
});
