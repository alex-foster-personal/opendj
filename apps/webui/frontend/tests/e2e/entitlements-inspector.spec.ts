// requirement: ADMIN-01
import { expect, test, type Page } from '@playwright/test';

async function gotoShellReady(page: Page, path: string): Promise<void> {
	await page.goto(path);
	await page.waitForFunction(() => (window as { __mdtPerfLog?: unknown }).__mdtPerfLog !== undefined);
}

test('admin entitlements inspector lists capabilities and live refusals', async ({ page }) => {
	await gotoShellReady(page, '/admin');

	const panel = page.getByTestId('entitlements-inspector');
	await expect(panel).toBeVisible();
	await expect(panel.getByRole('heading', { name: 'Entitlements inspector' })).toBeVisible();
	await expect(panel.getByText('jobs')).toBeVisible();
	await expect(panel.getByText('progressLedger')).toBeVisible();
	await expect(panel.getByText('progress ledger not offered by this daemon')).toBeVisible();
	await expect(panel.locator('.fatal')).toHaveCount(0);
	await expect(page.locator('#lyrics-generator')).toBeVisible();
});
