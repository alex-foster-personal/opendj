import { expect, test } from '@playwright/test';

// requirement: CSSTATUS-04
// [if] the CloudSync chip is healthy on first load [then] it is not in error state and keeps its navigable title, [else stop]
test('at 900px the CloudSync chip is a single-line link to /cloudsync', async ({ page }) => {
	await page.setViewportSize({ width: 900, height: 700 });
	await page.goto('/');

	const chip = page.getByRole('link', { name: 'CloudSync status' });
	await expect(chip).toBeVisible();
	await expect(chip.locator('.chip-label-short')).toHaveText('off');
	await expect(chip.locator('.chip-label-full')).toBeHidden();

	const box = await chip.boundingBox();
	expect(box).not.toBeNull();
	expect(box!.height).toBeLessThanOrEqual(24);
	expect(box!.width).toBeGreaterThan(box!.height);

	await expect(chip).toHaveAttribute('title', /CloudSync/);
	await expect(chip).toHaveAttribute('title', /Click to open CloudSync\./);
	await expect(chip).not.toHaveClass(/error/);
	await expect(chip.locator('.chip-label-short')).not.toHaveText('err');

	await chip.click();
	await expect(page).toHaveURL(/\/cloudsync/);
});

test('at 1280px the CloudSync chip shows the full label in one line', async ({ page }) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await page.goto('/');

	const chip = page.getByRole('link', { name: 'CloudSync status' });
	await expect(chip).toBeVisible();
	await expect(chip.locator('.chip-label-full')).toHaveText('sync: off');
	await expect(chip.locator('.chip-label-short')).toBeHidden();

	const box = await chip.boundingBox();
	expect(box).not.toBeNull();
	expect(box!.height).toBeLessThanOrEqual(24);
});
