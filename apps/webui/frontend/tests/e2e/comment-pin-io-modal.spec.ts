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
		// An empty draft is never parked (persistParkedPinDraft), so type first.
		await page.locator('.fb-bubble-text').fill('io modal anchor probe');
		await expect
			.poll(() => readParkedDraftAnchor(page), {
				message: 'draft anchor must name an element inside the I/O menu'
			})
			.toMatch(/hp-menu|hp-device|master output device/i);
	});

	test('an ordinary click outside the open I/O menu (not pin chrome) still closes it', async ({
		page
	}) => {
		// Opposite-direction control for the pin-arm exemption: only pin chrome
		// may keep the pinned I/O menu open; any other outside press closes it.
		const ioButton = page.getByRole('button', { name: 'SHOW AUDIO I/O' });
		await ioButton.click({ timeout: 5_000 });
		const masterSelect = page.getByLabel('master output device');
		await expect(masterSelect).toBeVisible();

		// An empty spot of the top bar: a point whose hit target IS the header.
		const spot = await page.evaluate(() => {
			const header = document.querySelector('header.rb-topbar');
			if (header === null) throw new Error('header.rb-topbar is not mounted');
			const r = header.getBoundingClientRect();
			const y = r.top + r.height / 2;
			for (let x = r.left + 2; x < r.right - 2; x += 2) {
				if (document.elementFromPoint(x, y) === header) return { x, y };
			}
			return null;
		});
		expect(spot, 'the top bar must expose an empty spot to click').not.toBeNull();
		const hit = await page.evaluate(
			({ x, y }) => {
				const el = document.elementFromPoint(x, y);
				return {
					pinChrome: el?.closest('.fb-place-skip, .fb-pin, .fb-cluster') != null,
					inMenu: el?.closest('.hp-menu, .explainer') != null
				};
			},
			spot!
		);
		expect(hit).toEqual({ pinChrome: false, inMenu: false });

		await page.mouse.click(spot!.x, spot!.y);
		await expect(masterSelect).toHaveCount(0);
		expect((await readFeedbackState(page)).placementArmed).toBe(false);
	});

	test('placement on open Settings overlay anchors inside the dialog, not main chrome', async ({
		page
	}) => {
		await page.keyboard.press('Control+,');
		const settings = page.getByRole('dialog', { name: 'Settings' });
		await expect(settings).toBeVisible({ timeout: 5_000 });

		const dockPin = page.locator('.fb-dock .fb-shell-pin');
		await expect(dockPin).toBeVisible();
		await dockPin.click();
		await expect
			.poll(async () => (await readFeedbackState(page)).placementArmed)
			.toBe(true);

		const search = page.getByRole('searchbox', { name: 'Search settings' });
		await expect(search).toBeVisible();
		const box = await search.boundingBox();
		expect(box).not.toBeNull();
		await page.mouse.click(box!.x + box!.width / 2, box!.y + box!.height / 2);

		await expect(page.locator('.fb-bubble')).toBeVisible({ timeout: 5_000 });
		// An empty draft is never parked (persistParkedPinDraft), so type first.
		await page.locator('.fb-bubble-text').fill('settings anchor probe');
		await expect
			.poll(() => readParkedDraftAnchor(page), {
				message: 'draft anchor must name an element inside Settings'
			})
			.toMatch(/so-search|Search settings|Settings/i);
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
