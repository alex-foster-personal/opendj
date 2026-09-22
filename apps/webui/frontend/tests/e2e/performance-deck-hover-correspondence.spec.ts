import { expect, test } from '@playwright/test';

// IOPIN-02; Source: comment-pin 990d39d59328. Real page/pointer/computed styles.
test('deck and mixer show the same conspicuous correspondence ring', async ({ page }) => {
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const deck = page.locator('.rb-deck[data-deck-hover="1"]');
	const channel = page.locator('[data-mixer-channel="1"]');
	await channel.hover();
	await expect(deck).toHaveClass(/deck-focus/);
	await expect(channel).toHaveClass(/deck-focus/);
	const ring = (el: Element) => {
		const s = getComputedStyle(el);
		return { color: s.outlineColor, width: s.outlineWidth, style: s.outlineStyle };
	};
	await expect.poll(() => channel.evaluate(ring)).toEqual(await deck.evaluate(ring));
	expect(await deck.evaluate(ring)).toMatchObject({ width: '2px', style: 'solid' });
	await deck.hover();
	await expect(channel).toHaveClass(/deck-focus/);
	await page.getByRole('button', { name: 'MIDI panel', exact: true }).hover();
	await expect(deck).not.toHaveClass(/deck-focus/);
	await expect(channel).not.toHaveClass(/deck-focus/);
});
