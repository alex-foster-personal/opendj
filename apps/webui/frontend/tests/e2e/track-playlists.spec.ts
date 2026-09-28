/**
 * LIBM-29: Show in playlists track menu navigates the tree to a live playlist.
 */
import { expect, test, type APIRequestContext } from '@playwright/test';

const API_TIMEOUT_MS = 10_000;
const PLAYLIST_ROW = '[data-testid="playlist-row"]';

type PlaylistCleanup = {
	playlistId: string;
	etag: string;
};

async function _createPlaylistWithTrack(
	request: APIRequestContext,
	name: string,
	stableId: string,
	cleanup: PlaylistCleanup[]
): Promise<void> {
	const created = await request.post('/api/v1/playlists', {
		data: { name },
		timeout: API_TIMEOUT_MS
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	const body = (await created.json()) as { playlist_id: string };
	const createEtag = created.headers().etag;
	expect(createEtag).toBeTruthy();

	const entry: PlaylistCleanup = { playlistId: body.playlist_id, etag: createEtag as string };
	cleanup.push(entry);

	const replaced = await request.put(`/api/v1/playlists/${body.playlist_id}/tracks`, {
		headers: { 'If-Match': createEtag as string },
		data: { stable_ids: [stableId] },
		timeout: API_TIMEOUT_MS
	});
	expect(replaced.ok(), await replaced.text()).toBeTruthy();
	const etag = replaced.headers().etag;
	expect(etag).toBeTruthy();
	entry.etag = etag as string;
}

async function _deletePlaylist(request: APIRequestContext, entry: PlaylistCleanup): Promise<void> {
	const deleted = await request.delete(`/api/v1/playlists/${entry.playlistId}`, {
		headers: { 'If-Match': entry.etag },
		timeout: API_TIMEOUT_MS
	});
	expect(deleted.ok(), await deleted.text()).toBeTruthy();
}

test('Show in playlists lists live memberships and navigates on click', async ({ page, request }) => {
	test.setTimeout(90_000);
	const stamp = Date.now();
	const nameA = `LIBM-29 A ${stamp}`;
	const nameB = `LIBM-29 B ${stamp}`;
	const cleanup: PlaylistCleanup[] = [];

	try {
		let stableId = '';
		await test.step('API setup: track and playlist memberships', async () => {
			const tracks = await request.get('/api/v1/tracks?limit=1&available=true', {
				timeout: API_TIMEOUT_MS
			});
			expect(tracks.ok(), await tracks.text()).toBeTruthy();
			const items = (await tracks.json()) as { items: Array<{ stable_id: string }> };
			expect(items.items.length).toBeGreaterThan(0);
			stableId = items.items[0]!.stable_id;
			expect(stableId).toBeTruthy();

			await _createPlaylistWithTrack(request, nameA, stableId, cleanup);
			await _createPlaylistWithTrack(request, nameB, stableId, cleanup);
		});

		await test.step('boot performance with playlists in tree', async () => {
			await page.goto('/performance');
			const targetRow = page.locator(`[data-testid="track-row"][data-stable-id="${stableId}"]`);
			await expect(targetRow).toBeVisible({ timeout: 30_000 });
			await expect(page.locator(PLAYLIST_ROW).filter({ hasText: nameA })).toBeVisible({
				timeout: 30_000
			});
			await expect(page.locator(PLAYLIST_ROW).filter({ hasText: nameB })).toBeVisible({
				timeout: 30_000
			});
		});

		await test.step('popover lists live memberships', async () => {
			const targetRow = page.locator(`[data-testid="track-row"][data-stable-id="${stableId}"]`);
			await targetRow.click({ button: 'right' });
			await page.getByRole('menuitem', { name: 'Show in playlists' }).click();

			const popover = page.locator('[data-testid="track-playlists-menu"]');
			await expect(popover).toBeVisible();
			await expect(popover).toContainText(nameA);
			await expect(popover).toContainText(nameB);
		});

		await test.step('navigation selects playlist in tree', async () => {
			const popover = page.locator('[data-testid="track-playlists-menu"]');
			await popover.getByRole('menuitem', { name: nameA }).click();
			const selectedRow = page.locator(`${PLAYLIST_ROW}.selected`, {
				hasText: nameA
			});
			await expect(selectedRow).toBeVisible({ timeout: 15_000 });
		});
	} finally {
		await test.step('API cleanup: delete playlists', async () => {
			for (const entry of cleanup) {
				await _deletePlaylist(request, entry);
			}
		});
	}
});
