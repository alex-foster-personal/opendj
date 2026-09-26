/**
 * Headed e2e for smartlist CRUD via tree context menu (LIBMX-01/04/05/10).
 */
import { expect, test, type Page } from '@playwright/test';

const TREE = '[data-testid="playlist-tree"]';
const ROW = '[data-testid="smartlist-row"]';
const MENU = '[data-testid="context-menu"]';
const FOLDER = '[data-testid="playlist-folder"]';

async function _smartlists(page: Page): Promise<Array<{ id: string; name: string; rule: unknown }>> {
	const response = await page.request.get('/api/v1/smartlists');
	expect(response.ok()).toBeTruthy();
	return (await response.json()) as Array<{ id: string; name: string; rule: unknown }>;
}

function _smartlistRow(page: Page, name: string) {
	return page.locator(ROW).filter({ has: page.getByText(name, { exact: true }) });
}

test.describe('smartlist tree CRUD', () => {
	test.beforeEach(async ({ page }) => {
		page.on('dialog', async (dialog) => {
			if (dialog.message().toLowerCase().includes('skip delete confirm')) {
				await dialog.dismiss();
				return;
			}
			await dialog.accept();
		});
	});

	// REQ: LIBMX-10
	test('new smartlist from Playlists tab context menu', async ({ page }) => {
		const name = `New SL ${Date.now()}`;
		const createdIds: string[] = [];
		try {
			await page.goto('/performance');
			await expect(page.locator(TREE)).toBeVisible({ timeout: 30_000 });

			await page.locator(FOLDER).click({ button: 'right' });
			await expect(page.locator(MENU)).toContainText('New smartlist');
			await page.getByRole('menuitem', { name: 'New smartlist' }).click();

			await expect(page.getByTestId('library-source-autolists')).toHaveAttribute(
				'aria-selected',
				'true'
			);
			const renameInput = page.getByLabel('Rename smartlist');
			await expect(renameInput).toBeFocused({ timeout: 30_000 });
			await renameInput.fill(name);
			await renameInput.press('Enter');

			await expect(_smartlistRow(page, name)).toBeVisible({ timeout: 30_000 });
			const rows = await _smartlists(page);
			const match = rows.find((row) => row.name === name);
			expect(match).toBeTruthy();
			if (match) createdIds.push(match.id);
			expect(page.url()).not.toContain('/smartlists');
		} finally {
			for (const id of createdIds) {
				await page.request.delete(`/api/v1/smartlists/${id}`);
			}
		}
	});

	test('rename and duplicate via Autolists context menu', async ({ page }) => {
		const base = `CRUD ${Date.now()}`;
		const renamed = `${base} renamed`;
		let sourceId: string | null = null;
		let copyId: string | null = null;
		try {
			const create = await page.request.post('/api/v1/smartlists', {
				data: { name: base, rule: { field: 'rating', op: '>=', value: 0 } }
			});
			expect(create.status()).toBe(201);
			sourceId = ((await create.json()) as { id: string }).id;

			await page.goto('/performance');
			await expect(page.locator(TREE)).toBeVisible({ timeout: 30_000 });
			await page.getByTestId('library-source-autolists').click();
			await expect(_smartlistRow(page, base)).toBeVisible({ timeout: 30_000 });

			await _smartlistRow(page, base).click({ button: 'right' });
			await page.getByRole('menuitem', { name: 'Rename' }).click();
			const renameInput = page.getByLabel('Rename smartlist');
			await renameInput.fill(renamed);
			await renameInput.press('Enter');
			await expect(_smartlistRow(page, renamed)).toBeVisible({ timeout: 30_000 });
			const afterRename = await _smartlists(page);
			expect(afterRename.some((row) => row.name === renamed)).toBeTruthy();
			expect(afterRename.some((row) => row.name === base)).toBeFalsy();

			await _smartlistRow(page, renamed).click({ button: 'right' });
			await page.getByRole('menuitem', { name: 'Duplicate', exact: true }).click();
			await expect(page.locator(ROW)).toHaveCount(2, { timeout: 30_000 });
			const afterDup = await _smartlists(page);
			expect(afterDup).toHaveLength(2);
			const source = afterDup.find((row) => row.id === sourceId);
			const copy = afterDup.find((row) => row.id !== sourceId);
			expect(source?.rule).toEqual(copy?.rule);
			copyId = copy?.id ?? null;
		} finally {
			for (const id of [sourceId, copyId]) {
				if (id) await page.request.delete(`/api/v1/smartlists/${id}`);
			}
		}
	});

	test('tree count matches GET tracks membership', async ({ page }) => {
		const name = `Count ${Date.now()}`;
		let id: string | null = null;
		try {
			const create = await page.request.post('/api/v1/smartlists', {
				data: {
					name,
					rule: { field: 'rating', op: '>=', value: 0 }
				}
			});
			expect(create.status()).toBe(201);
			id = ((await create.json()) as { id: string }).id;

			await page.goto('/performance');
			await page.getByTestId('library-source-autolists').click();
			const row = _smartlistRow(page, name);
			await expect(row).toBeVisible({ timeout: 30_000 });
			const countText = await row.locator('.count').innerText();
			const tracks = await page.request.get(`/api/v1/smartlists/${id}/tracks`);
			expect(tracks.ok()).toBeTruthy();
			const body = (await tracks.json()) as { items: unknown[] };
			expect(Number(countText)).toBe(body.items.length);
		} finally {
			if (id) await page.request.delete(`/api/v1/smartlists/${id}`);
		}
	});
});
