/**
 * PERF-UI-01 / issue #4051: Autolists smartlist scroller stays usable at 1280x720.
 *
 * [if] Autolists tab at 720p [then] scroll area and rows sit inside the window [else stop].
 *
 * The bottom-left link strip the rows once had to clear is gone (PERF-UI-07,
 * tests/e2e/performance-no-app-nav.spec.ts), so the bound is the window's own
 * bottom edge, and the hit test below proves nothing else covers the row.
 */
import { expect, test } from '@playwright/test';

test.use({ viewport: { width: 1280, height: 720 } });

const ROW = '[data-testid="smartlist-row"]';

test('autolists: smartlist scroll height and rows stay inside the window at 720p', async ({
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
			const scroll = document.querySelector('[data-testid="autolists-scroll"]');
			const smartRow = document.querySelector('[data-testid="smartlist-row"]');
			if (scroll === null || smartRow === null) {
				throw new Error('missing autolists elements');
			}
			return { windowBottom: window.innerHeight, rowBottom: smartRow.getBoundingClientRect().bottom };
		});
		expect(geometry.windowBottom, 'the 720p viewport this test sets').toBe(720);
		expect(geometry.rowBottom, 'smartlist row bottom edge').toBeLessThanOrEqual(geometry.windowBottom);

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
