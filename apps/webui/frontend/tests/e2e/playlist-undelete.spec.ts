/**
 * Headed e2e for playlist delete and restore via Recently deleted (LIBMX-03).
 */
import { expect, test, type Page } from '@playwright/test';

const TREE = '[data-testid="playlist-tree"]';
const ROW = '[data-testid="playlist-row"]';
const RECENTLY_DELETED = '[data-testid="playlist-recently-deleted"]';
const DELETED_ROW = '[data-testid="playlist-deleted-row"]';
const RESTORE = '[data-testid="restore-playlist"]';

async function _playlistNames(page: Page): Promise<string[]> {
	const response = await page.request.get('/api/v1/playlists');
	expect(response.ok()).toBeTruthy();
	const body = (await response.json()) as Array<{ name?: unknown }>;
	return body
		.map((row) => (typeof row.name === 'string' ? row.name : null))
		.filter((name): name is string => name !== null);
}

async function _deleteViaUi(page: Page, name: string): Promise<void> {
	await page
		.locator(ROW)
		.filter({ hasText: name })
		.getByTitle('Delete playlist')
		.click();
	const dialog = page.getByRole('dialog', { name: 'Delete playlist' });
	await expect(dialog).toBeVisible();
	await expect(dialog).toContainText(`Delete playlist "${name}"?`);
	await dialog.getByRole('button', { name: 'Delete', exact: true }).click();
	await expect(dialog).toBeHidden();
}

async function _hardDeleteByName(page: Page, name: string): Promise<void> {
	const response = await page.request.get('/api/v1/playlists');
	if (!response.ok()) return;
	const rows = (await response.json()) as Array<{ playlist_id?: unknown; name?: unknown }>;
	const match = rows.find((row) => row.name === name);
	if (match === undefined || typeof match.playlist_id !== 'string') return;
	const detail = await page.request.get(`/api/v1/playlists/${match.playlist_id}`);
	if (!detail.ok()) return;
	const etag = detail.headers().etag;
	if (etag === undefined || etag === '') return;
	await page.request.delete(`/api/v1/playlists/${match.playlist_id}`, {
		headers: { 'If-Match': etag }
	});
}

test('playlist tree: delete then restore from Recently deleted', async ({ page }) => {
	const playlistName = `Playlist undelete ${Date.now()}`;

	try {
		await page.goto('/performance');
		await expect(page.locator(TREE)).toBeVisible({ timeout: 30_000 });
		await expect(page.locator(ROW).first()).toBeVisible({ timeout: 30_000 });

		await page.locator('[data-testid="playlist-folder"]').getByTitle('Create playlist').click();
		const renameInput = page.getByRole('textbox', { name: 'Rename playlist' });
		await expect(renameInput).toBeVisible();
		await renameInput.fill(playlistName);
		await renameInput.press('Enter');
		await expect(page.locator(ROW).filter({ hasText: playlistName })).toBeVisible();

		const listResponse = await page.request.get('/api/v1/playlists');
		expect(listResponse.ok()).toBeTruthy();
		const playlists = (await listResponse.json()) as Array<{
			playlist_id?: unknown;
			name?: unknown;
		}>;
		const created = playlists.find((row) => row.name === playlistName);
		expect(created).toBeTruthy();
		const playlistId = created?.playlist_id;
		expect(typeof playlistId).toBe('string');

		const detailBefore = await page.request.get(`/api/v1/playlists/${playlistId}`);
		expect(detailBefore.ok()).toBeTruthy();
		const etag = detailBefore.headers().etag;
		expect(etag).toBeTruthy();

		const tracksResponse = await page.request.get('/api/v1/tracks?limit=2');
		expect(tracksResponse.ok()).toBeTruthy();
		const tracks = (await tracksResponse.json()) as { items?: Array<{ stable_id?: unknown }> };
		const stableIds = (tracks.items ?? [])
			.map((row) => (typeof row.stable_id === 'string' ? row.stable_id : null))
			.filter((id): id is string => id !== null)
			.slice(0, 2);
		expect(stableIds.length).toBeGreaterThanOrEqual(2);

		const putMembers = await page.request.put(`/api/v1/playlists/${playlistId}/tracks`, {
			headers: { 'If-Match': etag ?? '' },
			data: { stable_ids: stableIds }
		});
		expect(putMembers.ok()).toBeTruthy();

		const snapshot = await page.request.get(`/api/v1/playlists/${playlistId}`);
		expect(snapshot.ok()).toBeTruthy();
		const itemsBefore = ((await snapshot.json()) as { items?: unknown }).items;

		await _deleteViaUi(page, playlistName);
		await expect(page.locator(ROW).filter({ hasText: playlistName })).toHaveCount(0);
		expect(await _playlistNames(page)).not.toContain(playlistName);

		const deletedList = await page.request.get('/api/v1/playlists/deleted');
		expect(deletedList.ok()).toBeTruthy();
		const deletedRows = (await deletedList.json()) as Array<{ name?: unknown }>;
		expect(deletedRows.some((row) => row.name === playlistName)).toBeTruthy();

		await page.locator(RECENTLY_DELETED).click();
		const deletedRow = page.locator(DELETED_ROW).filter({ hasText: playlistName });
		await expect(deletedRow).toBeVisible();
		await deletedRow.locator(RESTORE).click();

		await expect(page.locator(ROW).filter({ hasText: playlistName })).toBeVisible();
		expect(await _playlistNames(page)).toContain(playlistName);

		const detailAfter = await page.request.get(`/api/v1/playlists/${playlistId}`);
		expect(detailAfter.ok()).toBeTruthy();
		expect(((await detailAfter.json()) as { items?: unknown }).items).toEqual(itemsBefore);

		const deletedAfter = await page.request.get('/api/v1/playlists/deleted');
		expect(deletedAfter.ok()).toBeTruthy();
		const deletedAfterRows = (await deletedAfter.json()) as Array<{ name?: unknown }>;
		expect(deletedAfterRows.some((row) => row.name === playlistName)).toBeFalsy();
	} finally {
		await _hardDeleteByName(page, playlistName);
	}
});
