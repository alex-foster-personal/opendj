// requirement: PERFMODE-16
// [if] user switches to Gig with unset helper pref [then] opt-in dialog appears

import { expect, test } from '@playwright/test';

test('gig helper prompt offers enable on first Gig switch', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	await page.getByRole('button', { name: 'Gig', exact: true }).click();
	const dialog = page.getByTestId('gig-helper-prompt');
	// 15s, not the 5s default: trunk red Tue 29 Sep 2026 (checkpoint 077b1358a81)
	// - the exact-SHA rerun passed clean and no gig-helper code changed between
	// the last green checkpoint and this one, so the 5s window was too tight
	// for a loaded runner's render, not a broken transition. Same allowance
	// pattern as brand-launch.spec.ts / artwork-reader-unavailable.spec.ts.
	await expect(dialog).toBeVisible({ timeout: 15_000 });
	await dialog.getByRole('button', { name: 'Enable', exact: true }).click();
	await expect(dialog).toBeHidden();
	await expect(page.getByTestId('gig-helper-on')).toBeVisible();
});

// [if] user picks No thanks [then] a later Gig switch does not reopen dialog, [else stop].
test('gig helper No thanks suppresses future prompts', async ({ page, request }) => {
	const put = await request.put('/api/v1/ui-prefs', {
		data: { gig_helper: 'unset', app_posture: 'prep' }
	});
	expect(put.ok()).toBeTruthy();

	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	await page.getByRole('button', { name: 'Gig', exact: true }).click();
	const dialog = page.getByTestId('gig-helper-prompt');
	// See the matching comment in the first test above.
	await expect(dialog).toBeVisible({ timeout: 15_000 });
	await dialog.getByRole('button', { name: 'No thanks', exact: true }).click();
	await expect(dialog).toBeHidden();

	await page.getByRole('button', { name: 'Prep', exact: true }).click();
	await page.getByRole('button', { name: 'Gig', exact: true }).click();
	await expect(dialog).toBeHidden();
});
