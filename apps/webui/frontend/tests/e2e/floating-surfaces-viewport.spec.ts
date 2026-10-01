// requirement: UX-FLOAT-01
import { expect, test } from '@playwright/test';

test.use({ viewport: { width: 1280, height: 720 } });

async function assertInsideWindow(
	page: import('@playwright/test').Page,
	locator: import('@playwright/test').Locator
): Promise<void> {
	await expect
		.poll(async () => locator.boundingBox(), {
			timeout: 5_000,
			message: 'surface must have geometry'
		})
		.not.toBeNull();

	await expect
		.poll(
			async () => {
				const polled = await locator.boundingBox();
				if (!polled) return null;
				const win = await page.evaluate(() => ({
					w: window.innerWidth,
					h: window.innerHeight
				}));
				return (
					polled.x >= 0 &&
					polled.y >= 0 &&
					polled.x + polled.width <= win.w &&
					polled.y + polled.height <= win.h
				);
			},
			{ timeout: 5_000 }
		)
		.toBe(true);
}

async function seedPinsVisible(page: import('@playwright/test').Page): Promise<void> {
	await page.addInitScript(async () => {
		const mod = await import(new URL('/src/lib/rb/feedback.ts', location.href).href);
		window.localStorage.setItem(mod.PINS_VISIBLE_KEY, mod.serializePinsVisible(true));
	});
}

async function readFeedbackAvailability(page: import('@playwright/test').Page): Promise<string> {
	return page.evaluate(async () => {
		const mod = await import(new URL('/src/lib/rb/feedback-store.svelte.ts', location.href).href);
		return mod.feedbackState.availability;
	});
}

test('hover tile, comment pin, and QuickDraw stay inside the viewport from the bottom-right corner', async ({
	page
}) => {
	test.setTimeout(90_000);
	await seedPinsVisible(page);
	await page.goto('/performance');
	await page.getByText('All Tracks', { exact: true }).first().click();
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await expect.poll(async () => await readFeedbackAvailability(page), { timeout: 10_000 }).toBe('ok');

	const brX = 1280 - 4;
	const brY = 720 - 4;

	// FB-18c (#3981) docks an always-on pin + support pair in the bottom-right
	// corner at the root overlay layer, so it owns the corner pixels this
	// test probes and intercepts every hover and click aimed there. The dock
	// is not the subject here (the surfaces' viewport clamp is), so take it
	// out of hit-testing the same way the pins are hidden further down.
	const dock = page.locator('.fb-dock');
	await expect(dock).toBeAttached();
	await dock.evaluate((el) => {
		(el as HTMLElement).style.visibility = 'hidden';
	});

	const analysisTrigger = page
		.locator('[aria-label="Analysis coverage"], [aria-label="Data-quality issues"]')
		.first();
	await analysisTrigger.evaluate(
		(el, pos) => {
			el.style.position = 'fixed';
			el.style.left = `${pos.x}px`;
			el.style.top = `${pos.y}px`;
			el.style.zIndex = '99999';
		},
		{ x: 1280 - 28, y: 720 - 28 }
	);
	await analysisTrigger.hover();
	const hoverTile = page.getByTestId('analysis-dots-pop');
	await expect(hoverTile).toBeVisible();
	await assertInsideWindow(page, hoverTile);
	await page.mouse.move(0, 0);
	await expect(hoverTile).toHaveCount(0, { timeout: 2_000 });

	// Topbar pin, not FB-18c's dock pin of the same name (#3981).
	await page.getByRole('banner').getByRole('button', { name: 'Drop a comment pin' }).click();
	await expect(page.locator('.fb-place-overlay')).toBeVisible({ timeout: 10_000 });
	await page.locator('.fb-place-overlay').click({ position: { x: brX, y: brY } });
	await page.locator('.fb-bubble-text').fill('viewport clamp probe');
	await page.locator('.fb-mini', { hasText: 'Save pin' }).click();
	await expect(page.locator('.fb-bubble')).toHaveCount(0, { timeout: 10_000 });
	const pinMarker = page.locator('.fb-pin[title^="viewport clamp probe"]');
	await expect(pinMarker).toHaveCount(1);
	await pinMarker.click({ delay: 50 });
	const pinBody = page.locator('.fb-pin-body').last();
	await expect(pinBody).toBeVisible();
	await assertInsideWindow(page, pinBody);
	await page.keyboard.press('Escape');
	await page.mouse.move(0, 0);
	// FB-16 (#3888) draws pins in the app-root layer above .perf-root, so the
	// probe pin now owns this corner pixel; hide pins so the right-click
	// reaches the performance surface QuickDraw listens on.
	await page.evaluate(() => {
		const twin = (window as unknown as Record<string, unknown>).__mdtPinsVisible as
			| { set: (v: boolean) => void }
			| undefined;
		if (twin === undefined) throw new Error('__mdtPinsVisible twin is not installed');
		twin.set(false);
	});
	await expect(pinMarker).toHaveCount(0);

	await page.locator('.perf-root').click({ button: 'right', position: { x: brX, y: brY } });
	const quickDraw = page.getByTestId('quick-draw-menu');
	await expect(quickDraw).toBeVisible();
	for (const selector of ['.qd', '.qd-quick', '.qd-mid', '.qd-leaf', '.qd-ctx']) {
		const part = quickDraw.locator(selector);
		if ((await part.count()) === 0) continue;
		await assertInsideWindow(page, part.first());
	}
	await page.keyboard.press('Escape');
});
