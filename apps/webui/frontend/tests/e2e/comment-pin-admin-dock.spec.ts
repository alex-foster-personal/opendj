import { expect, test } from '@playwright/test';

/** FB-18c: feedback dock on admin routes arms comment-pin placement. */

async function readPlacementArmed(page: import('@playwright/test').Page): Promise<boolean> {
	return page.evaluate(async () => {
		const mod = await import(new URL('/src/lib/rb/feedback-store.svelte.ts', location.href).href);
		return mod.feedbackState.placementArmed;
	});
}

test('admin page shows feedback dock and arms placement', async ({ page }) => {
	await page.goto('/admin');
	await expect(page.getByLabel('Drop a comment pin').last()).toBeVisible({ timeout: 15_000 });
	await page.getByLabel('Drop a comment pin').last().click();
	await expect.poll(async () => readPlacementArmed(page)).toBe(true);
});

test('admin feedback dock opens support panel from bottom-right', async ({ page }) => {
	await page.goto('/admin');
	const support = page.getByRole('button', { name: 'Open support and feedback' });
	await expect(support).toBeVisible({ timeout: 15_000 });
	await support.click();
	await expect(page.getByRole('dialog', { name: 'Support and feedback' })).toBeVisible({
		timeout: 5_000
	});
});
