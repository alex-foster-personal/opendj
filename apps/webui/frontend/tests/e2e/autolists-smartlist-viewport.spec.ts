/**
 * PERF-UI-01 / issue #4051: Autolists smartlist scroller stays usable at 1280x720.
 *
 * [if] Autolists tab at 720p [then] scroll area and rows sit above app nav [else stop].
 */
import { expect, test } from '@playwright/test';

test.use({ viewport: { width: 1280, height: 720 } });

const ROW = '[data-testid="smartlist-row"]';

test('autolists: smartlist scroll height and rows stay above performance app nav at 720p', async ({
	page
}) => {
	const name = `Autolists viewport ${Date.now()}`;
	let createdId: string | null = null;

	try {
		const create = await page.request.post('/api/v1/smartlists', {
			data: { name, rule: { field: 'rating', op: '>=', value: 0 } }
		});
		expect(create.status()).toBe(201);
		const body = (await create.json()) as { id?: unknown };
		expect(typeof body.id).toBe('string');
		createdId = body.id as string;

		await page.goto('/performance');
		await page.getByTestId('library-source-autolists').click();

		const scrollHeight = await page.getByTestId('autolists-scroll').evaluate((el) => {
			return el.getBoundingClientRect().height;
		});
		expect(scrollHeight).toBeGreaterThanOrEqual(44);

		const row = page.locator(ROW).filter({ has: page.getByText(name, { exact: true }) });
		await expect(row).toBeVisible({ timeout: 30_000 });

		const geometry = await page.evaluate(() => {
			const nav = document.querySelector('[data-testid="performance-app-nav"]');
			const scroll = document.querySelector('[data-testid="autolists-scroll"]');
			const smartRow = document.querySelector('[data-testid="smartlist-row"]');
			if (nav === null || scroll === null || smartRow === null) {
				throw new Error('missing autolists or nav elements');
			}
			const navBox = nav.getBoundingClientRect();
			const rowBox = smartRow.getBoundingClientRect();
			return { navTop: navBox.top, rowBottom: rowBox.bottom, scrollHeight: scroll.getBoundingClientRect().height };
		});
		expect(geometry.rowBottom).toBeLessThanOrEqual(geometry.navTop - 2);

		const hit = await row.evaluate((el) => {
			const box = el.getBoundingClientRect();
			const x = box.left + box.width / 2;
			const y = box.top + box.height / 2;
			const top = document.elementFromPoint(x, y);
			return top === el || el.contains(top);
		});
		expect(hit).toBe(true);
	} finally {
		if (createdId !== null) {
			await page.request.delete(`/api/v1/smartlists/${createdId}`);
		}
	}
});
