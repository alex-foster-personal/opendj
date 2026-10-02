import { expect, test, type Page } from '@playwright/test';

// requirement: DECKUX-12
// [if] /performance is 1280px or 1440px wide [then] Create pairing is visible,
// on one line, passes a real elementFromPoint hit test and the top bar does not
// overflow [else stop]. Before this, a 1740px media rule hid it below 1741px.
// [if] fewer than two decks are loaded [then] the button is disabled and its
// title says why, instead of failing after the click [else stop].

const PAIRING = 'button.topbar-slot-pairing';
const SHOT_DIR = process.env.PAIRING_SHOT_DIR;

async function _hit(page: Page, selector: string): Promise<boolean> {
	return page.locator(selector).evaluate((element) => {
		const rect = element.getBoundingClientRect();
		const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
		return hit === element || element.contains(hit);
	});
}

test('Create pairing stays reachable on one line at 1280px and 1440px, and at full width', async ({
	page
}) => {
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	const button = page.locator(PAIRING);

	for (const width of [1280, 1440, 1920]) {
		await page.setViewportSize({ width, height: 800 });
		await expect(button, `${width}px: Create pairing is hidden`).toBeVisible();
		await expect.poll(() => _hit(page, PAIRING), { message: `${width}px: not hittable` }).toBe(true);
		const box = await button.boundingBox();
		expect(box, `${width}px: no box`).not.toBeNull();
		// One text line is ~17px here; the wrapped two-line label measured 28px.
		expect(box!.height, `${width}px: the label wrapped`).toBeLessThan(22);
		const overflow = await page
			.locator('header.rb-topbar')
			.evaluate((bar) => bar.scrollWidth - bar.clientWidth);
		expect(overflow, `${width}px: the top bar overflows by ${overflow}px`).toBeLessThanOrEqual(0);
		await expect(button).toHaveAccessibleName('Create pairing');
		if (SHOT_DIR) {
			await page.screenshot({
				path: `${SHOT_DIR}/pairing-button-${width}px.png`,
				clip: { x: Math.max(0, box!.x - 360), y: 0, width: 720, height: 60 }
			});
		}
	}

	// No deck is loaded in this fixture boot: the button says why it cannot open.
	await expect(button).toBeDisabled();
	await expect(button).toHaveAttribute('title', /Load a track on two decks/);
});
