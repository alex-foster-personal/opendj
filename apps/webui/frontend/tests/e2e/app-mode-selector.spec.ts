// requirement: PERFMODE-13
// [if] the mode dropdown opens [then] four tiles render with icons and copy

import { expect, test } from '@playwright/test';

const EXPECTED_LABELS = ['Gig', 'Prep', 'Library', 'Trackify'] as const;
const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

test('mode picker shows four tiles and persists library selection', async ({ page }) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	const picker = page.locator('details.mode-picker');
	await picker.locator('summary[aria-label="Choose app mode"]').click();
	await expect(picker).toHaveAttribute('open', '');

	const cards = picker.locator('a.mode-card[data-testid="mode-card"]');
	await expect(cards).toHaveCount(4);

	for (let i = 0; i < EXPECTED_LABELS.length; i++) {
		const card = cards.nth(i);
		await expect(card.locator('strong')).toHaveText(EXPECTED_LABELS[i]);
		await expect(card.locator('.mode-thumbnail')).toBeVisible();
		await expect(card.locator('[data-testid="mode-gain"]')).not.toBeEmpty();
		await expect(card.locator('[data-testid="mode-lose"]')).not.toBeEmpty();
	}

	const libraryCard = cards.filter({ hasText: 'Library' });
	await libraryCard.click();
	await page.waitForURL((url) => url.pathname === '/');

	const stored = await page.evaluate((key) => {
		const raw = localStorage.getItem(key);
		if (raw === null) return null;
		return JSON.parse(raw) as { app_mode?: string };
	}, STORAGE_KEY);
	expect(stored?.app_mode).toBe('library');
});
