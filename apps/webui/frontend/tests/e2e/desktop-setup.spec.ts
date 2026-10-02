/**
 * Desktop shell setup screen: operator copy stays human-safe; agent details
 * live in the closed disclosure.
 */
import { expect, test } from '@playwright/test';

import { DEAD_ENGINE_ORIGIN, DEAD_ENGINE_PORT } from './playwright.desktop-setup.config';

const HEALTH_URL = `${DEAD_ENGINE_ORIGIN}/api/v1/health`;
const SETUP_URL = `/index.html?engine=${encodeURIComponent(DEAD_ENGINE_ORIGIN)}`;

test.describe('desktop shell setup screen', () => {
	test.beforeAll(async () => {
		let reachable = false;
		try {
			await fetch(HEALTH_URL, { signal: AbortSignal.timeout(2000) });
			reachable = true;
		} catch {
			reachable = false;
		}
		expect(
			reachable,
			`port ${DEAD_ENGINE_PORT} must stay unbound for this suite to mean anything`
		).toBe(false);
	});

	test('shows friendly copy by default and agent details only when opened', async ({
		page
	}) => {
		await page.goto(SETUP_URL);

		const root = page.locator('#root');
		await expect(root).toHaveAttribute('data-state', 'unreachable');

		await expect(page.locator('#checking-view')).toBeHidden();
		await expect(page.locator('#unreachable-view')).toBeVisible();

		await expect(page.getByRole('heading', { name: 'Open DJ cannot reach its engine' })).toBeVisible();

		const visibleBody = await page.locator('#unreachable-view').innerText();
		expect(visibleBody).not.toMatch(/127\.0\.0\.1/);
		expect(visibleBody).not.toMatch(/\/api\/v1\//);
		expect(visibleBody).not.toMatch(/OPENDJ_ENGINE_ORIGIN/);
		expect(visibleBody).not.toMatch(/apps\.engine_core serve/);

		const attempts = Number(await page.locator('#attempts').innerText());
		expect(attempts).toBeGreaterThanOrEqual(1);

		await expect(page.locator('#attempts')).toHaveAttribute('title', /how many times/i);

		await page.locator('#agent-details').click();

		await expect(page.locator('#probe-url')).toHaveText(HEALTH_URL);
		await expect(page.locator('#probe-detail')).not.toHaveText('(unknown)');
		await expect(page.locator('#probe-detail')).toContainText('no response');
		await expect(page.locator('#engine-command')).toContainText(`--port ${DEAD_ENGINE_PORT}`);
		await expect(page.locator('#engine-command')).toContainText('apps.engine_core serve');

		await expect(page.locator('body')).not.toContainText('No track loaded');
	});

	test('check now re-probes rather than dead-ending', async ({ page }) => {
		await page.goto(SETUP_URL);
		await expect(page.locator('#root')).toHaveAttribute('data-state', 'unreachable');

		const before = Number(await page.locator('#attempts').innerText());
		await page.getByRole('button', { name: 'Check now' }).click();

		await expect
			.poll(async () => Number(await page.locator('#attempts').innerText()), {
				timeout: 10_000
			})
			.toBeGreaterThan(before);

		await expect(page.locator('#root')).toHaveAttribute('data-state', 'unreachable');
	});
});
