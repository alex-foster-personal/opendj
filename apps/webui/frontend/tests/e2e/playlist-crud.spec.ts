/**
 * Headed e2e for playlist create / duplicate / delete in the tree (#168).
 *
 * Acceptance:
 * - [if] create does not appear in both tree and GET /api/v1/playlists [then stop]
 * - [if] duplicate does not appear as "<name> (copy)" in tree and API [then stop]
 * - [if] reload drops either playlist [then stop]
 * - [if] delete leaves a row behind in tree or API [then stop]
 */
import { expect, test, type Page } from '@playwright/test';

const TREE = '[data-testid="playlist-tree"]';
const ROW = '[data-testid="playlist-row"]';

async function _playlistNames(page: Page): Promise<string[]> {
	const response = await page.request.get('/api/v1/playlists');
	expect(response.ok()).toBeTruthy();
	const body = (await response.json()) as Array<{ name?: unknown }>;
	return body
		.map((row) => (typeof row.name === 'string' ? row.name : null))
		.filter((name): name is string => name !== null);
}

async function _deleteByName(page: Page, name: string): Promise<void> {
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

function _playlistRow(page: Page, name: string) {
	return page.locator(ROW).filter({ has: page.getByText(name, { exact: true }) });
}

async function _deleteViaUi(page: Page, name: string): Promise<void> {
	await _playlistRow(page, name).getByTitle('Delete playlist').click();
	const dialog = page.getByRole('dialog', { name: 'Delete playlist' });
	await expect(dialog).toBeVisible();
	await expect(dialog).toContainText(`Delete playlist "${name}"?`);
	await dialog.getByRole('button', { name: 'Delete', exact: true }).click();
	await expect(dialog).toBeHidden();
}

test('playlist tree: create, duplicate, reload persistence, and delete', async ({ page }) => {
	const baseName = `Playlist CRUD ${Date.now()}`;
	const copyName = `${baseName} (copy)`;

	try {
		await page.goto('/performance');
		await expect(page.locator(TREE)).toBeVisible({ timeout: 30_000 });
		await expect(page.locator(ROW).first()).toBeVisible({ timeout: 30_000 });

		await page.locator('[data-testid="playlist-folder"]').getByTitle('Create playlist').click();
		const renameInput = page.getByRole('textbox', { name: 'Rename playlist' });
		await expect(renameInput).toBeVisible();
		await renameInput.fill(baseName);
		await renameInput.press('Enter');
		await expect(_playlistRow(page, baseName)).toBeVisible();
		expect(await _playlistNames(page)).toContain(baseName);

		await _playlistRow(page, baseName).click({ button: 'right' });
		await page.getByRole('menuitem', { name: 'Duplicate', exact: true }).click();
		await expect(_playlistRow(page, copyName)).toBeVisible();
		expect(await _playlistNames(page)).toContain(copyName);

		await page.reload();
		await expect(page.locator(TREE)).toBeVisible({ timeout: 30_000 });
		await expect(_playlistRow(page, baseName)).toBeVisible();
		await expect(_playlistRow(page, copyName)).toBeVisible();
		const afterReload = await _playlistNames(page);
		expect(afterReload).toContain(baseName);
		expect(afterReload).toContain(copyName);

		await _deleteViaUi(page, baseName);
		await expect(_playlistRow(page, baseName)).toHaveCount(0);
		expect(await _playlistNames(page)).not.toContain(baseName);
		await expect(_playlistRow(page, copyName)).toBeVisible();

		await _deleteViaUi(page, copyName);
		await expect(_playlistRow(page, copyName)).toHaveCount(0);
		expect(await _playlistNames(page)).not.toContain(copyName);
	} finally {
		await _deleteByName(page, baseName);
		await _deleteByName(page, copyName);
	}
});
