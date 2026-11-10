/**
 * requirement: AISIDE-11839
 *
 * The performance top bar's OJ button opens the agent sidebar as a right-edge
 * overlay (#9477, epic #9374 O5).
 *
 * Regression lines:
 * - if the assistant is configured and the OJ button is clicked and the deck area's boxes move then broken
 * - if the assistant is unconfigured and an OJ button renders then broken
 * - if a deck is playing and the sidebar opens with an animation then broken
 * - if a second click or Esc in the empty composer leaves the sidebar open then broken
 * - if an agent's oj_toggle does not open the same sidebar a click does then broken
 */
import { expect, test, type Page } from '@playwright/test';

const BUTTON = '[data-testid="oj-topbar-button"]';
const DOCK = '.assistant-dock';
const DECK_PROBES = ['[data-testid="play-deck-1"]', '[data-testid="master-deck-1"]'];

async function stubAssistant(page: Page, configured: boolean): Promise<void> {
	await page.route('**/api/v1/flags', async (route) => {
		const response = await route.fetch();
		const body = await response.json();
		const flags = (body.flags ?? []).filter(
			(flag: { flag_id: string }) => flag.flag_id !== 'assistant.sidebar'
		);
		flags.push({ flag_id: 'assistant.sidebar', enabled: true, default: false, overridden: true });
		await route.fulfill({ response, json: { ...body, flags } });
	});
	await page.route('**/api/v1/assistant/status', (route) =>
		route.fulfill({
			json: configured
				? { configured: true, model: 'test/model', mode: 'byok', code: null }
				: { configured: false, model: 'test/model', mode: null, code: 'assistant_key_missing' }
		})
	);
}

async function gotoPerformance(page: Page): Promise<void> {
	await page.setViewportSize({ width: 1920, height: 1080 });
	await page.goto('/performance?muted=1');
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);
}

async function deckBoxes(page: Page) {
	return Promise.all(DECK_PROBES.map((sel) => page.locator(sel).first().boundingBox()));
}

test('AISIDE-11839: unconfigured assistant shows no OJ button', async ({ page }) => {
	await stubAssistant(page, false);
	await gotoPerformance(page);
	await page.waitForTimeout(1_000);
	await expect(page.locator(BUTTON)).toHaveCount(0);
});

test('AISIDE-11839: the OJ button toggles the sidebar over the decks without reflow', async ({
	page
}) => {
	test.setTimeout(120_000);
	await stubAssistant(page, true);
	await gotoPerformance(page);
	await expect(page.locator(BUTTON)).toBeVisible({ timeout: 30_000 });

	const before = await deckBoxes(page);
	await page.locator(BUTTON).click();
	await expect(page.locator(DOCK)).toBeVisible({ timeout: 30_000 });
	await expect(page.locator(BUTTON)).toHaveAttribute('aria-pressed', 'true');
	expect(await deckBoxes(page), 'opening the sidebar must not reflow the decks').toEqual(before);

	await page.locator(BUTTON).click();
	await expect(page.locator(DOCK)).toBeHidden();

	// Esc in the empty composer closes it too (AISIDE-7892f rule).
	await page.locator(BUTTON).click();
	await expect(page.locator(DOCK)).toBeVisible();
	await page.locator(`${DOCK} textarea`).first().focus();
	await page.keyboard.press('Escape');
	await expect(page.locator(DOCK)).toBeHidden();
});

test('AISIDE-11839: an agent oj_toggle opens the same sidebar, still while a deck plays', async ({
	page
}) => {
	test.setTimeout(120_000);
	await stubAssistant(page, true);
	await gotoPerformance(page);
	await expect
		.poll(async () => page.locator('[data-testid="track-row"]').count(), { timeout: 30_000 })
		.toBeGreaterThan(0);
	const sid = await page
		.locator('[data-testid="track-row"]')
		.first()
		.getAttribute('data-stable-id');
	await page.evaluate(async (stable_id) => {
		await window.musicDjToolsPerformance!.dispatch({
			type: 'load',
			deck: 1,
			stable_id: stable_id!
		});
		await window.musicDjToolsPerformance!.dispatch({ type: 'play', deck: 1, playing: true });
	}, sid);
	await expect
		.poll(() => page.evaluate(() => window.musicDjToolsPerformance!.query().decks[1].playing), {
			timeout: 45_000
		})
		.toBe(true);

	await page.evaluate(() => window.musicDjToolsPerformance!.dispatch({ type: 'oj_toggle' }));
	await expect(page.locator(DOCK)).toBeVisible({ timeout: 30_000 });
	await expect(page.locator(DOCK)).toHaveAttribute('data-oj-sidebar-motion', 'still');

	await page.evaluate(() => window.musicDjToolsPerformance!.dispatch({ type: 'oj_toggle' }));
	await expect(page.locator(DOCK)).toBeHidden();
	await page.evaluate(() =>
		window.musicDjToolsPerformance!.dispatch({ type: 'play', deck: 1, playing: false })
	);
});
