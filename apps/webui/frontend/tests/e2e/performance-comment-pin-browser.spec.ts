import { expect, test } from '@playwright/test';

// IOPIN-01: exercise real keyboard/DOM focus and production browse adapter.
test('browser selection scrolls, MIDI reclaims tracks, and sliders keep arrows', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const rows = page.getByTestId('track-row');
	await expect(rows.first()).toBeVisible();
	await rows.first().click();
	for (let i = 0; i < 25; i++) await page.keyboard.press('ArrowDown');
	const selected = page.locator('[data-testid="track-row"].rb-row-selected');
	await expect(selected).toBeVisible();
	const before = await selected.getAttribute('data-stable-id');
	const slider = page.getByRole('slider', { name: 'master volume', exact: true });
	await slider.focus();
	await page.keyboard.press('ArrowLeft');
	await expect(slider).toBeFocused();
	await expect(selected).toHaveAttribute('data-stable-id', before!);
	await page.evaluate(async () => {
		const url = '/src/lib/rb/midi/browse-adapter.ts';
		const { getBrowseAdapter } = await import(url);
		getBrowseAdapter().moveSelection(1);
	});
	await expect(selected).not.toHaveAttribute('data-stable-id', before!);
	await expect(selected).toBeVisible();
	// A/D are the horizontal counterparts of W/S, not silently ignored.
	await selected.focus();
	await page.keyboard.press('d');
	await expect(selected.locator('button.deck-target').first()).toBeFocused();
});
