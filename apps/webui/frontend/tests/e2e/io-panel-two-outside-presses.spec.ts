/**
 * IOPIN-20 browser acceptance (pin 5d0378165889): with the Audio I/O panel
 * open, the FIRST primary press outside it still reaches its target and the
 * panel stays open; the SECOND press outside closes it.
 *
 * `tests/unit/output-menu-explainer-placement.test.mjs` only reads
 * `HeadphoneCluster.svelte` source for the capture listener and the counter.
 * This spec presses real pointer events on the rendered page (root
 * `playwright.config.ts`: fixture library, engine + vite), so capture
 * ordering, the panel containment check and the counter all have to work.
 *
 * The outside targets are library track rows: a row press selects that row
 * (`aria-selected`), which is an effect the panel could only block by eating
 * the event.
 *
 * RUN: pnpm exec playwright test io-panel-two-outside-presses
 *
 * Acceptance:
 * - [if] the first outside press closes the panel [then] one click on a deck or row dismisses I/O - broken
 * - [if] the first outside press does not reach its target [then] the open panel blocks the decks - broken
 * - [if] the second outside press leaves the panel open [then] the operator has to hunt for X - broken
 */
import { expect, test, type Page } from '@playwright/test';

const TRACK_ROW = '[data-testid="track-row"]';

/** Wait until the open panel stops changing size: a section that loads on
 * demand (the remote speaker, I18) can grow it over a point found earlier. */
async function _panelSettled(page: Page): Promise<void> {
	const panel = page.locator('[data-audio-io-panel]');
	let last = '';
	await expect
		.poll(
			async () => {
				const box = JSON.stringify(await panel.boundingBox());
				const settled = box === last;
				last = box;
				return settled;
			},
			{ timeout: 20_000, intervals: [250] }
		)
		.toBe(true);
}

/** Center of row `index`, checked to hit the row itself and not the panel. */
async function _outsidePoint(page: Page, index: number): Promise<{ x: number; y: number }> {
	await _panelSettled(page);
	const point = await page.locator(TRACK_ROW).nth(index).evaluate((row) => {
		const r = row.getBoundingClientRect();
		// Several cells across the row, in case the panel covers part of it.
		for (const fx of [0.5, 0.35, 0.65, 0.2, 0.8]) {
			const x = r.left + r.width * fx;
			const y = r.top + r.height / 2;
			const hit = document.elementFromPoint(x, y);
			if (hit !== null && row.contains(hit) && hit.closest('[data-audio-io-panel]') === null) return { x, y };
		}
		return null;
	});
	expect(point, `track row ${index} must expose a point not covered by the I/O panel`).not.toBeNull();
	return point!;
}

test('IOPIN-20: the first outside press reaches its target and keeps I/O open; the second closes it', async ({
	page
}) => {
	// A cold vite compile of /performance alone can take most of the 30 s default.
	test.setTimeout(60_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect.poll(async () => page.locator(TRACK_ROW).count(), { timeout: 30_000 }).toBeGreaterThanOrEqual(2);

	const rowA = page.locator(TRACK_ROW).nth(0);
	const rowB = page.locator(TRACK_ROW).nth(1);
	await expect(rowA).toHaveAttribute('aria-selected', 'false');
	await expect(rowB).toHaveAttribute('aria-selected', 'false');

	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await expect(panel).toBeVisible();

	// First outside press: it lands on row A, and the panel stays.
	const first = await _outsidePoint(page, 0);
	await page.mouse.click(first.x, first.y);
	await expect(
		rowA,
		'if the first outside press does not reach its target, the open panel blocks the decks - broken'
	).toHaveAttribute('aria-selected', 'true');
	// Give a wrongly armed close time to land before asserting it did not.
	await page.waitForTimeout(300);
	await expect(panel, 'if the first outside press closes the panel, one click dismisses I/O - broken').toBeVisible();

	// Second outside press: it also reaches row B, and the panel closes.
	const second = await _outsidePoint(page, 1);
	await page.mouse.click(second.x, second.y);
	await expect(rowB).toHaveAttribute('aria-selected', 'true');
	await expect(
		panel,
		'if the second outside press leaves the panel open, the operator has to hunt for X - broken'
	).toBeHidden();
});

test('IOPIN-20 control: a press inside the panel resets the count, so it takes two fresh outside presses', async ({
	page
}) => {
	// A cold vite compile of /performance alone can take most of the 30 s default.
	test.setTimeout(60_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
	await expect.poll(async () => page.locator(TRACK_ROW).count(), { timeout: 30_000 }).toBeGreaterThanOrEqual(2);

	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await expect(panel).toBeVisible();

	// Each press re-finds an uncovered point: a section that loads on demand
	// (the remote speaker, I18) can grow the panel over a point found earlier.
	const pressOutside = async (): Promise<void> => {
		const outside = await _outsidePoint(page, 1);
		await page.mouse.click(outside.x, outside.y);
	};
	await pressOutside();
	await expect(panel).toBeVisible();

	// A press on the panel's own header text is inside: the count starts over.
	await panel.locator('.hp-panel-header strong').click();
	await pressOutside();
	await page.waitForTimeout(300);
	await expect(panel, 'an inside press must reset the count; one more outside press must not close').toBeVisible();

	await pressOutside();
	await expect(panel).toBeHidden();
});

/** A primary pointerdown whose target is the page root, as a native select
 * menu's hit (or a press on bare page background) reports it.
 *
 * Dispatched, not pressed with page.mouse: at this viewport no point of the
 * page hit-tests to body or html (`_rootHitPoints` asserts that), and headless
 * Chromium draws no OS select menu whose hit could produce one. If the layout
 * ever exposes a root point, `_rootHitPoints` fails and this should become a
 * real page.mouse press there. */
async function _rootPress(page: Page): Promise<void> {
	await page.evaluate(() => {
		document.body.dispatchEvent(new PointerEvent('pointerdown', { button: 0, bubbles: true, cancelable: true }));
	});
}

/** Every 10 px grid point that hit-tests to body or html. */
async function _rootHitPoints(page: Page): Promise<number> {
	return page.evaluate(() => {
		let n = 0;
		for (let y = 0; y < window.innerHeight; y += 10)
			for (let x = 0; x < window.innerWidth; x += 10) {
				const hit = document.elementFromPoint(x, y);
				if (hit === document.body || hit === document.documentElement) n += 1;
			}
		return n;
	});
}

/** A real press on the select (opens its menu), then Escape closes the menu;
 * the select keeps focus, as after picking a device. */
async function _useSelect(page: Page, select: import('@playwright/test').Locator): Promise<void> {
	await select.click();
	await page.keyboard.press('Escape');
	await expect(select).toBeFocused();
}

test('IOPIN-20: a focused select in the panel does not swallow later root presses (Sol P1 4285298949)', async ({
	page
}) => {
	test.setTimeout(60_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await expect(panel).toBeVisible();
	const select = panel.locator('select').first();
	await expect(select, 'the panel must render a device select to test').toBeVisible();

	expect(await _rootHitPoints(page), 'no real page.mouse press can hit the root here, so it is dispatched').toBe(0);
	// A real press on the select, its menu canceled with Escape: the select keeps
	// focus, and no press inside the panel comes between (Sol P1 4286270421).
	await _useSelect(page, select);
	await expect(panel).toBeVisible();
	await _rootPress(page);
	await page.waitForTimeout(300);
	await expect(panel, 'the first root press counts once and keeps the panel').toBeVisible();
	await _rootPress(page);
	await expect(
		panel,
		'if a focused select makes root presses vanish, the promised second press leaves I/O open - broken'
	).toBeHidden();
});

test('IOPIN-20 control: the root hit right after pressing a select is its menu, and is skipped once', async ({
	page
}) => {
	test.setTimeout(60_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await expect(panel).toBeVisible();
	const select = panel.locator('select').first();
	await expect(select).toBeVisible();

	// A real press on the select opens its native menu; on a desktop the menu's
	// hit then lands on the root, which is what the dispatched press stands for.
	await select.click();
	await _rootPress(page);
	await _rootPress(page);
	await page.waitForTimeout(300);
	await expect(panel, 'the menu hit must not count: one real outside press so far').toBeVisible();
	await _rootPress(page);
	await expect(panel).toBeHidden();
});

test('IOPIN-20: a select menu closed by pressing the select again does not swallow a later root press (Sol P1 4286270421)', async ({
	page
}) => {
	test.setTimeout(60_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await expect(panel).toBeVisible();
	const select = panel.locator('select').first();
	await expect(select).toBeVisible();

	// Open the menu, then close it with a second press on the same select.
	await select.click();
	await select.click();
	await _rootPress(page);
	await page.waitForTimeout(300);
	await expect(panel, 'the first root press counts once and keeps the panel').toBeVisible();
	await _rootPress(page);
	await expect(panel, 'if the canceled menu swallows a root press, it takes three - broken').toBeHidden();
});

test('IOPIN-20: reopening a select menu canceled with Escape skips its menu hit once (Codex P2 4286965506)', async ({
	page
}) => {
	test.setTimeout(60_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.goto('/performance');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	await page.getByRole('button', { name: 'SHOW AUDIO I/O' }).click();
	const panel = page.getByRole('dialog', { name: 'Audio I/O settings' });
	await expect(panel).toBeVisible();
	const select = panel.locator('select').first();
	await expect(select).toBeVisible();

	// Open, cancel with Escape, then press the same select again: that press
	// reopens the menu, so the next root hit is the menu's and is skipped.
	await _useSelect(page, select);
	await select.click();
	expect(await select.evaluate((el) => el.matches(':open')), 'the second press must reopen the menu').toBe(true);
	await _rootPress(page);
	await _rootPress(page);
	await page.waitForTimeout(300);
	await expect(
		panel,
		'if the reopened menu hit counts as an outside press, the first real one closes I/O - broken'
	).toBeVisible();
	await _rootPress(page);
	await expect(panel).toBeHidden();
});
