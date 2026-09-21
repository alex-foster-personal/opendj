// requirement: ADMIN-03
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

	// The inspector lists a refused capability twice on purpose: once in the
	// full resolved set, once in the active-refusal subset. Name which block
	// each assertion means, so the duplicate is proof rather than ambiguity.
	const resolved = panel.getByTestId('inspector-resolved-flags');
	const refusals = panel.getByTestId('inspector-active-refusals');
	await expect(resolved.getByText('jobs')).toBeVisible();
	await expect(resolved.getByText('progressLedger')).toBeVisible();
	await expect(refusals.getByText('progressLedger')).toHaveCount(0);
	// "Offered" is rendered by the inspector as the row's title sentence and the
	// absence of a refusal span; the `progress ledger: offered` line lives on the
	// Diagnostics tab, which this page is not on (admin opens on the KPI tab).
	const ledgerRow = resolved.locator('.row', { hasText: 'progressLedger' });
	await expect(ledgerRow).toHaveCount(1);
	await expect(ledgerRow.locator('.value')).toHaveText('true');
	await expect(ledgerRow.locator('.refusal')).toHaveCount(0);
	await expect(ledgerRow).toHaveAttribute('title', 'progressLedger: true. Offered.');
	await expect(panel.locator('.fatal')).toHaveCount(0);
	await expect(page.locator('#lyrics-generator')).toBeVisible();
});
