import { expect, test } from '@playwright/test';

/**
 * FB-16 criteria 1 and 5: dropping a pin on the Audio I/O modal anchors an
 * element inside the modal, and the feedback explainer stays beside it.
 *
 * Runs under playwright.comment-hotkey-gate.config.ts (real backend + fixture
 * library), same as comment-hotkey-browser.spec.ts.
 */

async function readFeedbackState(
	page: import('@playwright/test').Page
): Promise<{ availability: string; placementArmed: boolean }> {
	return page.evaluate(async () => {
		const mod = await import(new URL('/src/lib/rb/feedback-store.svelte.ts', location.href).href);
		return {
			availability: mod.feedbackState.availability,
			placementArmed: mod.feedbackState.placementArmed
		};
	});
}

async function readParkedDraftAnchor(page: import('@playwright/test').Page): Promise<string | null> {
	return page.evaluate(() => {
		const raw = localStorage.getItem('odj-feedback-pin-draft');
		if (raw === null) return null;
		const parsed = JSON.parse(raw) as { anchor?: string | null };
		return parsed.anchor ?? null;
	});
}

test.describe('comment pin on Audio I/O modal', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/performance');
		await expect
			.poll(async () => (await readFeedbackState(page)).availability, { timeout: 10_000 })
			.toBe('ok');
	});

	test('placement on an open I/O select anchors inside the hp-menu, not main chrome', async ({
		page
	}) => {
		const ioButton = page.getByRole('button', { name: 'SHOW AUDIO I/O' });
		await ioButton.click({ timeout: 5_000 });
		const menu = page.locator('.hp-menu');
		await expect(menu).toBeVisible();

		await page.getByRole('button', { name: 'Drop a comment pin' }).click();
		await expect
			.poll(async () => (await readFeedbackState(page)).placementArmed)
			.toBe(true);

		const masterSelect = page.getByLabel('master output device');
		await expect(masterSelect).toBeVisible();
		const box = await masterSelect.boundingBox();
		expect(box).not.toBeNull();
		await page.mouse.click(box!.x + box!.width / 2, box!.y + box!.height / 2);

		await expect(page.locator('.fb-bubble')).toBeVisible({ timeout: 5_000 });
		const anchor = await readParkedDraftAnchor(page);
		expect(anchor, 'draft anchor must name an element inside the I/O menu').toMatch(/hp-menu|hp-device|master output device/i);
	});

	test('placement on open Settings overlay anchors inside the dialog, not main chrome', async ({
		page
	}) => {
		await page.keyboard.press('Control+,');
		const settings = page.getByRole('dialog', { name: 'Settings' });
		await expect(settings).toBeVisible({ timeout: 5_000 });

		const floatPin = page.locator('.fb-pin-affordance-float .fb-shell-pin');
		await expect(floatPin).toBeVisible();
		await floatPin.click();
		await expect
			.poll(async () => (await readFeedbackState(page)).placementArmed)
			.toBe(true);

		const search = page.getByRole('searchbox', { name: 'Search settings' });
		await expect(search).toBeVisible();
		const box = await search.boundingBox();
		expect(box).not.toBeNull();
		await page.mouse.click(box!.x + box!.width / 2, box!.y + box!.height / 2);

		await expect(page.locator('.fb-bubble')).toBeVisible({ timeout: 5_000 });
		const anchor = await readParkedDraftAnchor(page);
		expect(anchor, 'draft anchor must name an element inside Settings').toMatch(
			/so-search|Search settings|Settings/i
		);
	});

	test('feedback explainer does not overlap the open Audio I/O menu', async ({ page }) => {
		const ioButton = page.getByRole('button', { name: 'SHOW AUDIO I/O' });
		await ioButton.click({ timeout: 5_000 });
		const menu = page.locator('.hp-menu');
		await expect(menu).toBeVisible();

		const commentBtn = page.getByRole('button', { name: 'Drop a comment pin' });
		await commentBtn.hover();
		const explainerPop = page.locator('.fb-cluster .pop');
		await expect(explainerPop).toBeVisible({ timeout: 5_000 });

		const menuBox = await menu.boundingBox();
		const popBox = await explainerPop.boundingBox();
		expect(menuBox).not.toBeNull();
		expect(popBox).not.toBeNull();
		const overlaps =
			popBox!.x < menuBox!.x + menuBox!.width &&
			popBox!.x + popBox!.width > menuBox!.x &&
			popBox!.y < menuBox!.y + menuBox!.height &&
			popBox!.y + popBox!.height > menuBox!.y;
		expect(overlaps, 'feedback explainer must not cover the I/O menu').toBe(false);
	});
});
