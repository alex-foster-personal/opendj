/**
 * Headed e2e for smartlist delete via tree context menu (LIBM-83).
 *
 * Acceptance:
 * - [if] right-click Delete on a smartlist row [then] row gone from tree and API
 */
import { expect, test, type Page } from '@playwright/test';

const TREE = '[data-testid="playlist-tree"]';
const ROW = '[data-testid="smartlist-row"]';
const MENU = '[data-testid="context-menu"]';

async function _smartlistIds(page: Page): Promise<string[]> {
	const response = await page.request.get('/api/v1/smartlists');
	expect(response.ok()).toBeTruthy();
	const body = (await response.json()) as Array<{ id?: unknown }>;
	return body
		.map((row) => (typeof row.id === 'string' ? row.id : null))
		.filter((id): id is string => id !== null);
}

function _smartlistRow(page: Page, name: string) {
	return page.locator(ROW).filter({ has: page.getByText(name, { exact: true }) });
}

// A 1000px viewport, not the root config's 720: at 1280x720 the /performance
// library pane is 88px tall, the Autolists browser (Genre/Rating/BPM) keeps
// its 64px, and the smartlist scroller below it is left 4px, so its rows sit
// under the bottom app nav and a right-click lands on the nav. That passed by
// 1px until #4004's 3px-taller source toolbar (measured Sat 26 Sep 2026:
// scroller y=698..702 before, y=701..705 after, nav at 702). These specs test
// the smartlist context menu, not that layout; the cramped pane is issue #4051.
test.use({ viewport: { width: 1280, height: 1000 } });

test('smartlist tree: context-menu delete removes row from tree and API', async ({ page }) => {
	const name = `Smartlist delete ${Date.now()}`;
	let createdId: string | null = null;

	page.on('dialog', async (dialog) => {
		if (dialog.message().toLowerCase().includes('skip delete confirm')) {
			await dialog.dismiss();
			return;
		}
		await dialog.accept();
	});

	try {
		await page.goto('/performance');
		await expect(page.locator(TREE)).toBeVisible({ timeout: 30_000 });

		const create = await page.request.post('/api/v1/smartlists', {
			data: { name, rule: { field: 'rating', op: '>=', value: 0 } }
		});
		expect(create.status()).toBe(201);
		const body = (await create.json()) as { id?: unknown };
		expect(typeof body.id).toBe('string');
		createdId = body.id as string;

		await page.reload();
		await expect(page.locator(TREE)).toBeVisible({ timeout: 30_000 });
		await page.getByTestId('library-source-autolists').click();
		await expect(_smartlistRow(page, name)).toBeVisible({ timeout: 30_000 });

		await _smartlistRow(page, name).click({ button: 'right' });
		await expect(page.locator(MENU)).toContainText('Delete');
		await page.getByRole('menuitem', { name: 'Delete' }).click();

		await expect(_smartlistRow(page, name)).toHaveCount(0);
		const ids = await _smartlistIds(page);
		expect(ids).not.toContain(createdId);
		createdId = null;
	} finally {
		if (createdId !== null) {
			await page.request.delete(`/api/v1/smartlists/${createdId}`);
		}
	}
});
