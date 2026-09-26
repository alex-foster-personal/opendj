// requirement: PERFMODE-16
// [if] user switches to Gig with unset helper pref [then] opt-in dialog appears

import { expect, test } from '@playwright/test';

test('gig helper prompt offers enable on first Gig switch', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	await page.getByRole('button', { name: 'Gig', exact: true }).click();
	const dialog = page.getByTestId('gig-helper-prompt');
	await expect(dialog).toBeVisible();
	await dialog.getByRole('button', { name: 'Enable', exact: true }).click();
	await expect(dialog).toBeHidden();
	await expect(page.getByTestId('gig-helper-on')).toBeVisible();
});
