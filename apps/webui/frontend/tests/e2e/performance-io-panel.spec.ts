import { expect, test } from '@playwright/test';

test('I/O hover is brief and click opens persistent settings with routing inside', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const trigger = page.getByRole('button', { name: 'SHOW AUDIO I/O' });
	await page.mouse.move(0, 0);
	await trigger.hover();
	const hint = page.getByRole('tooltip', { name: 'Audio I/O quick settings' });
	await expect(hint).toBeVisible();
	await page.mouse.move(0, 0);
	await expect(hint).toBeHidden();

	await trigger.click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await expect(panel).toBeVisible();
	await expect(panel.getByRole('button', { name: 'Close audio I/O settings' })).toBeVisible();
	await expect(panel.getByText('Esc or X to dismiss')).toBeVisible();
	await expect(panel.getByRole('button', { name: 'Practice output mode' })).toBeVisible();
	await expect(panel.getByRole('button', { name: 'Split cable output mode' })).toBeVisible();
	await expect(panel.getByText('BOOTH / MONITOR')).toBeVisible();
	await expect(panel.getByText('Advanced channel assignment')).toBeVisible();
	await expect(panel.getByRole('button', { name: 'BOOTH / MONITOR' })).toBeDisabled();
	await expect(panel.getByRole('button', { name: 'Advanced channel assignment' })).toBeDisabled();
	await expect(panel.getByLabel('master output device')).toBeVisible();
	await expect(panel.getByLabel('headphone output device', { exact: true })).toBeVisible();
	await expect(panel.getByLabel('audio input device')).toBeVisible();
	await expect(page.locator('.hp').getByRole('button', { name: 'Practice output mode' })).toHaveCount(0);
	await page.keyboard.press('Escape');
	await expect(panel).toBeHidden();
	await trigger.click();
	await expect(panel).toBeVisible();
	await panel.getByRole('button', { name: 'Close audio I/O settings' }).click();
	await expect(panel).toBeHidden();
});

test('I/O mode and delay controls dispatch into the shared engine read model', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await panel.getByRole('button', { name: 'Split cable output mode' }).click();
	await expect.poll(() => page.evaluate(() => window.musicDjToolsPerformance?.query().mixer.headphones.output_mode)).toBe('split_cable');
	await panel.getByRole('button', { name: 'Two outputs output mode' }).click();
	await expect.poll(() => page.evaluate(() => window.musicDjToolsPerformance?.query().mixer.headphones.output_mode)).toBe('two_outputs');
	const delay = panel.getByRole('spinbutton', { name: 'head delay milliseconds' });
	await delay.fill('37');
	await expect.poll(() => page.evaluate(() => window.musicDjToolsPerformance?.query().mixer.headphones.head_delay_ms)).toBe(37);
	await delay.press('ArrowUp');
	await expect.poll(() => page.evaluate(() => window.musicDjToolsPerformance?.query().mixer.headphones.head_delay_ms)).toBe(38);
});

test('I/O keeps calibration and selectors visible together', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await expect(panel.getByRole('button', { name: 'CALIBRATE CUE ALIGNMENT' })).toBeVisible();
	await expect(panel.getByLabel('master output device')).toBeVisible();
});
