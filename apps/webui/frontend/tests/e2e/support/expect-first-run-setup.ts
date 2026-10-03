/**
 * Staged assertion for PREFLIGHT-03 / issue #2722: prove the app decided to open
 * first-run setup within one health-probe cycle, then wait for the lazy overlay
 * chunk without folding Vite cold-compile time into the open decision budget.
 */
import { expect, type Locator, type Page } from '@playwright/test';

/** Budget for the app to show setup loading or the final dialog (one probe cycle). */
const SETUP_OPEN_DECISION_BUDGET_MS = 10_000;

/** Separate budget for dev-only lazy compilation of SetupOverlay.svelte. */
const SETUP_DIALOG_RENDER_BUDGET_MS = 30_000;

export async function expectFirstRunSetup(page: Page): Promise<Locator> {
	const setupDialog = page.getByRole('dialog', { name: 'First-run setup' });
	const setupLoading = page.getByRole('status', { name: 'Setup is loading' });
	const setupFailed = page.getByRole('alertdialog', { name: 'Setup failed to load' });
	const blockingGate = page.locator('[data-preflight-blocking="true"]');

	const openSignal = setupDialog.or(setupLoading).first();
	await expect(openSignal).toBeVisible({ timeout: SETUP_OPEN_DECISION_BUDGET_MS });

	await expect(blockingGate).toHaveCount(0);

	const dialogOrFailure = setupDialog.or(setupFailed);
	await expect(dialogOrFailure).toBeVisible({ timeout: SETUP_DIALOG_RENDER_BUDGET_MS });

	if (await setupFailed.isVisible()) {
		throw new Error(
			'First-run setup failed to load: "Setup failed to load" alertdialog is visible',
		);
	}
	await expect(setupDialog).toBeVisible();
	return setupDialog;
}
