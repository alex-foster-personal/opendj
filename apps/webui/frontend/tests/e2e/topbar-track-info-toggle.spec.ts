/**
 * requirement: TOPVIEW-05
 *
 * The Track information (i) button is a toggle even while the overlay's lazy
 * chunk is still loading (Sol P1 4287872605 on #6824). The two clicks are
 * dispatched in one task, so the chunk cannot resolve between them on any
 * machine: the second click always lands while the first open is pending.
 *
 * Regression lines:
 * - if a second click during the first open's import leaves the overlay open then broken
 * - if a single click does not open the overlay then broken (control)
 * - if an agent's track_info_toggle does not open the same overlay a click does then broken
 * - if the open overlay shows the loading or error state instead of the deck's real record then
 *   broken (Codex P1 4302745972: a visible dialog shell alone proves neither)
 * - if track_info_toggle opens an overlay that is not visible where the width tier hides the (i)
 *   button then broken (Sol P1 4350988015: the overlay sat inside the hidden slot)
 */
import { expect, test, type Page } from '@playwright/test';

const BUTTON = '[data-testid="topbar-track-info"]';
const OVERLAY = '[data-testid="track-info-overlay"]';

/**
 * The overlay is showing deck 1's real record, read through the app's own
 * record and play-count requests: its heading is the loaded track's title, its
 * rows and Beatgrid line rendered, and no error state is up.
 */
async function expectLoadedRecord(page: Page): Promise<void> {
	const title = await page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1].title);
	expect(title, 'the fixture track loaded on deck 1 has a title').toBeTruthy();
	await expect(page.locator(`${OVERLAY} [data-testid="track-info-title"]`)).toHaveText(title!, {
		timeout: 30_000
	});
	await expect(page.locator(`${OVERLAY} [data-testid="track-info-rows"] dt`).first()).toHaveText(
		'Title'
	);
	await expect(page.locator(`${OVERLAY} [data-testid="track-info-grid"]`)).toBeVisible();
	await expect(page.locator(`${OVERLAY} [data-testid="track-info-error"]`)).toHaveCount(0);
}

async function loadDeckOne(page: Page, viewport = { width: 1920, height: 1080 }): Promise<void> {
	await page.setViewportSize(viewport);
	await page.goto('/performance?muted=1');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect
		.poll(async () => page.locator('[data-testid="track-row"]').count(), { timeout: 30_000 })
		.toBeGreaterThan(0);
	const sid = await page
		.locator('[data-testid="track-row"]')
		.first()
		.getAttribute('data-stable-id');
	expect(sid, 'the fixture library exposes a track row').toBeTruthy();
	await page.evaluate(async (stable_id) => {
		await window.musicDjToolsPerformance!.dispatch({
			type: 'load',
			deck: 1,
			stable_id: stable_id!
		});
	}, sid);
	await expect(page.locator(BUTTON)).toBeEnabled({ timeout: 45_000 });
}

test('TOPVIEW-05: a second click while the overlay chunk loads closes it', async ({ page }) => {
	test.setTimeout(120_000);
	await loadDeckOne(page);

	const pending = await page.locator(BUTTON).evaluate((btn) => {
		(btn as HTMLButtonElement).click();
		const afterFirst = btn.getAttribute('aria-expanded');
		(btn as HTMLButtonElement).click();
		return { afterFirst, afterSecond: btn.getAttribute('aria-expanded') };
	});
	test.info().annotations.push({ type: 'aria-expanded', description: JSON.stringify(pending) });

	// Let the chunk import settle: once it has, the overlay would render if open.
	await page.waitForFunction(async () => {
		await import(new URL('/src/lib/components/rb/TrackInfoOverlay.svelte', location.href).href);
		return true;
	});
	await page.waitForTimeout(250);
	await expect(page.locator(BUTTON)).toHaveAttribute('aria-expanded', 'false');
	await expect(
		page.locator(OVERLAY),
		"if a second click during the first open's import leaves the overlay open then broken"
	).toHaveCount(0);

	// Control: one click opens it, and a second click (chunk now cached) closes it.
	await page.locator(BUTTON).click();
	await expect(page.locator(OVERLAY)).toBeVisible();
	await expectLoadedRecord(page);
	await page.locator(BUTTON).click();
	await expect(page.locator(OVERLAY)).toHaveCount(0);
});

test('TOPVIEW-05: an agent opens and closes the same overlay through the dispatcher (Codex P1 4300879440)', async ({
	page
}) => {
	test.setTimeout(120_000);
	await loadDeckOne(page);

	await page.evaluate(() =>
		window.musicDjToolsPerformance!.dispatch({ type: 'track_info_toggle' })
	);
	await expect(page.locator(OVERLAY)).toBeVisible();
	await expectLoadedRecord(page);
	await expect(page.locator(BUTTON)).toHaveAttribute('aria-expanded', 'true');
	const opened = await page.evaluate(() => window.musicDjToolsPerformance!.query().track_info);
	expect(opened).toMatchObject({ open: true, deck: 1 });

	await page.evaluate(() => window.musicDjToolsPerformance!.dispatch({ type: 'track_info_close' }));
	await expect(page.locator(OVERLAY)).toHaveCount(0);
	const closed = await page.evaluate(() => window.musicDjToolsPerformance!.query().track_info);
	expect(closed).toEqual({ open: false, deck: null, stable_id: null });
});

test('TOPVIEW-05: where the width tier hides the (i) button, track_info_toggle still shows the record (Sol P1 4350988015)', async ({
	page
}) => {
	test.setTimeout(120_000);
	// 1366 px is in the 1530 px tier that hides the utility slot.
	await loadDeckOne(page, { width: 1366, height: 768 });
	// Control: the tier really hides the button here.
	await expect(page.locator(BUTTON)).toBeHidden();

	await page.evaluate(() =>
		window.musicDjToolsPerformance!.dispatch({ type: 'track_info_toggle' })
	);
	await expect(
		page.locator(OVERLAY),
		'if track_info_toggle opens an overlay that is not visible where the width tier hides the (i) button then broken'
	).toBeVisible();
	await expectLoadedRecord(page);
	await page.evaluate(() => window.musicDjToolsPerformance!.dispatch({ type: 'track_info_close' }));
	await expect(page.locator(OVERLAY)).toHaveCount(0);
});
