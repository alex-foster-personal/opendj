import { expect, test } from '@playwright/test';

import { spendBootLanding } from './support/boot-landing';

// The chip under test lives in the APP-SHELL header, and both tests open `/`
// cold: without this the PERFMODE-11 landing redirect can detach the chip
// mid-click. support/boot-landing.ts has the full account.
test.beforeEach(async ({ page }) => {
	await spendBootLanding(page);
});

// requirement: CSSTATUS-04
// [if] the CloudSync chip is healthy on first load [then] it is not in error state and keeps its quick-actions title, [else stop]
test('at 900px the CloudSync chip is a single-line quick-actions trigger', async ({ page }) => {
	await page.setViewportSize({ width: 900, height: 700 });
	await page.goto('/');

	const chip = page.getByRole('button', { name: 'CloudSync status' });
	await expect(chip).toBeVisible();
	await expect(chip.locator('.chip-label-short')).toHaveText('off');
	await expect(chip.locator('.chip-label-full')).toBeHidden();

	const box = await chip.boundingBox();
	expect(box).not.toBeNull();
	expect(box!.height).toBeLessThanOrEqual(24);
	expect(box!.width).toBeGreaterThan(box!.height);

	await expect(chip).toHaveAttribute('title', /CloudSync/);
	await expect(chip).toHaveAttribute('title', /Click for quick actions\./);
	await expect(chip).not.toHaveClass(/error/);
	await expect(chip.locator('.chip-label-short')).not.toHaveText('err');

	await chip.click();
	await expect(page.getByTestId('cloudsync-quick-actions-popover')).toBeVisible();
	await expect(page).not.toHaveURL(/\/cloudsync/);
});

test('at 1280px the CloudSync chip shows the full label in one line', async ({ page }) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/');

	const chip = page.getByRole('button', { name: 'CloudSync status' });
	await expect(chip).toBeVisible();
	await expect(chip.locator('.chip-label-full')).toHaveText('sync: off');
	await expect(chip.locator('.chip-label-short')).toBeHidden();

	const box = await chip.boundingBox();
	expect(box).not.toBeNull();
	expect(box!.height).toBeLessThanOrEqual(24);
});
