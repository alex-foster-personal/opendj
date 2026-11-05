/**
 * Rendered contrast of the top bar's hover menus in every skin (UX-MENU-01).
 *
 * W34 (orders board #5638, Tue 6 Oct 2026) measured the Light skin's AutoPlay
 * and LYR/Stage menus at 1.27:1 (headings, labels) and 2.74:1 (status lines):
 * `.ap-menu` painted a literal #0a0c0f card under Light's dark ink. Those menus
 * open on `pointerenter` in JS, not CSS `:hover`, which is why the existing
 * rendered audit never saw them open. So this test opens each one through a
 * real pointer hover, asserts a `pointerenter` actually reached the trigger,
 * and reads the colors Chromium paints.
 *
 * [if] any text in either menu reads under 4.5:1 on the menu's painted card in
 *   Default, Gothic or Light [then] stop.
 * [if] a menu opens without a pointerenter on its trigger [then] stop: the test
 *   is no longer measuring the path users take.
 * control [if] the menu card is transparent [then] stop: the ratio would be
 *   computed against nothing.
 *
 * Mutation checks (Wed 7 Oct 2026), both red: with `.ap-menu { background:
 * #0a0c0f }` restored, the Light AutoPlay heading fails at 1.27:1; with LYR's
 * `onpointerenter` handler removed, the LYR menu never opens on hover.
 */
// requirement: UX-MENU-01
import { expect, test, type Page } from '@playwright/test';

const SKINS = ['default', 'mono-dev', 'light', 'green-room'] as const;
const MENUS = [
	{
		name: 'AutoPlay',
		trigger: 'span.ap-wrap',
		menu: '.ap-menu:not(.lyr-menu)'
	},
	{ name: 'LYR', trigger: 'span.lyr-wrap', menu: '[data-testid="lyr-menu"]' }
] as const;

async function setSkin(page: Page, skin: string): Promise<void> {
	const toggle = page.locator('[data-skin-current]');
	for (let i = 0; i < SKINS.length && (await toggle.getAttribute('data-skin-current')) !== skin; i++) {
		await toggle.click();
	}
	await expect(toggle).toHaveAttribute('data-skin-current', skin);
}

test('top bar hover menus meet AA text contrast in every skin', async ({ page }) => {
	await page.setViewportSize({ width: 1440, height: 800 });
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	for (const skin of SKINS) {
		await setSkin(page, skin);
		for (const { name, trigger, menu } of MENUS) {
			// Park the pointer away so the next hover is a fresh pointerenter.
			await page.mouse.move(4, 790);
			await expect(page.locator(menu)).toHaveCount(0);
			await page.locator(trigger).evaluate((el) => {
				(el as HTMLElement).dataset.sawPointerenter = '';
				el.addEventListener('pointerenter', () => ((el as HTMLElement).dataset.sawPointerenter = '1'), { once: true });
			});
			await page.locator(trigger).hover();
			await expect(page.locator(menu)).toBeVisible();
			await expect(page.locator(trigger), `${skin} ${name}: opened without a pointerenter`).toHaveAttribute(
				'data-saw-pointerenter',
				'1'
			);

			const measured = await page.locator(menu).evaluate((card) => {
				const rgb = (s: string) => (s.match(/[\d.]+/g) ?? []).map(Number);
				const lum = (s: string) => {
					const [r, g, b] = rgb(s).map((v) => {
						const c = v / 255;
						return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
					});
					return 0.2126 * r + 0.7152 * g + 0.0722 * b;
				};
				const bg = getComputedStyle(card).backgroundColor;
				const texts = [...card.querySelectorAll<HTMLElement>('p, .ap-row > span')].filter(
					(el) => el.textContent?.trim() && el.getClientRects().length > 0
				);
				return {
					bg,
					bgAlpha: rgb(bg)[3] ?? 1,
					probes: texts.map((el) => {
						const fg = getComputedStyle(el).color;
						const [a, b] = [lum(fg), lum(bg)];
						return {
							cls: el.className.split(' ')[0],
							fg,
							ratio: (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
						};
					})
				};
			});

			const label = `${skin} ${name} menu (card ${measured.bg})`;
			expect(measured.bgAlpha, `${label}: card is transparent`).toBe(1);
			expect(measured.probes.length, `${label}: no text measured`).toBeGreaterThan(1);
			for (const p of measured.probes) {
				expect(p.ratio, `${label}: .${p.cls} ${p.fg} reads ${p.ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
			}
		}
	}
	// ui_skin is a persisted pref on the shared backend: hand later specs the default.
	await setSkin(page, 'default');
});

/**
 * UX-MENU-02: the pointer can travel from each trigger down into its menu.
 * Sentry on #5743 (r4202411643): the card sits ~6px below the trigger, and
 * crossing that gap fired pointerleave on the wrap, so the menu closed before
 * the pointer reached it. Measured at 598af46c4: both menus closed at y=23.
 *
 * [if] a 1px-step walk from the trigger's center into the card closes the
 *   menu [then] stop.
 * control [if] the menu stays open after the pointer leaves for the page
 *   below [then] stop: the bridge must not hold the menu open everywhere.
 *
 * Mutation check (Wed 7 Oct 2026): without the `.ap-menu::before` bridge,
 * both walks close the menu at y=23.
 */
test('the pointer can walk from each top bar trigger into its hover menu', async ({ page }) => {
	await page.setViewportSize({ width: 1440, height: 800 });
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance?.version === 1);

	for (const { name, trigger, menu } of MENUS) {
		await page.mouse.move(4, 790);
		await expect(page.locator(menu)).toHaveCount(0);
		const t = await page.locator(`${trigger} > button`).first().boundingBox();
		if (t === null) throw new Error(`${name}: trigger not rendered`);
		const x = t.x + t.width / 2;
		await page.mouse.move(x, t.y + t.height / 2);
		await expect(page.locator(menu)).toBeVisible();
		const m = await page.locator(menu).boundingBox();
		if (m === null) throw new Error(`${name}: menu not rendered`);
		const walkX = Math.min(Math.max(x, m.x + 10), m.x + m.width - 10);
		for (let y = Math.round(t.y + t.height / 2); y <= m.y + 10; y++) {
			await page.mouse.move(walkX, y);
			expect(await page.locator(menu).count(), `${name}: menu closed at y=${y} on the way in`).toBe(1);
		}

		await page.mouse.move(walkX, 790);
		await expect(page.locator(menu), `${name}: menu stayed open after the pointer left`).toHaveCount(0);
	}
});
