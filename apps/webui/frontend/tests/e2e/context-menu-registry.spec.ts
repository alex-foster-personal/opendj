/**
 * W28 (CTXMENU-06): one smoke per right-click target kind the registry knows.
 *
 * Each test right-clicks a real element on /performance (root fixture engine,
 * no intercepted responses) and asserts two things:
 *   1. OUR menu shows the registry's items for that target, or, for an
 *      explicitly empty target, no menu at all;
 *   2. the browser's own menu is suppressed: a synthetic, cancelable
 *      `contextmenu` dispatched on the same element comes back
 *      defaultPrevented.
 *
 * Acceptance:
 * - [if] a hot-cue pad shows deck items (Unload CH1) [then stop]
 * - [if] a mixer strip shows "No target actions" [then stop]
 * - [if] a taglist / source tab right-click shows the deck launcher [then stop]
 * - [if] any target lets the browser menu through [then stop]
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';

const MENU = '[data-testid="context-menu"]';
const QUICK_DRAW = '[data-testid="quick-draw-menu"]';
// The first visible read-only tree element. Recently Deleted carries its
// marker on a display:contents wrapper (no box to click), so its header row
// stands in for it.
const READONLY_TREE =
	'[data-menu-target="tree-readonly"]:not(.subtree), ' +
	'[data-menu-target="tree-readonly"] > [data-testid="playlist-recently-deleted"]';

async function openPerformance(page: Page): Promise<void> {
	await page.goto('/performance');
	await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible({ timeout: 30_000 });
	await waitForPerformanceIpc(page);
}

/** True when a cancelable contextmenu on this element is prevented (no browser menu). */
async function browserMenuSuppressed(target: Locator): Promise<boolean> {
	return target.evaluate((el) => {
		const rect = el.getBoundingClientRect();
		const event = new MouseEvent('contextmenu', {
			bubbles: true,
			cancelable: true,
			button: 2,
			clientX: rect.left + rect.width / 2,
			clientY: rect.top + rect.height / 2
		});
		return !el.dispatchEvent(event);
	});
}

async function closeMenus(page: Page): Promise<void> {
	await page.keyboard.press('Escape');
	await expect(page.locator(MENU)).toHaveCount(0);
	await expect(page.locator(QUICK_DRAW)).toHaveCount(0);
}

test.describe('W28 right-click registry smokes', () => {
	test.beforeEach(async ({ page }) => {
		await openPerformance(page);
	});

	test('track row', async ({ page }) => {
		const row = page.locator('[data-testid="track-row"]').first();
		await row.click();
		await row.click({ button: 'right' });
		await expect(page.locator(MENU)).toContainText('Load to deck 1');
		await expect(page.getByRole('menuitem', { name: 'Re-analyze' })).toBeVisible();
		await expect(page.locator(QUICK_DRAW)).toHaveCount(0);
		await closeMenus(page);
		expect(await browserMenuSuppressed(row)).toBe(true);
		await closeMenus(page);
	});

	test('playlist row and the Playlists header', async ({ page }) => {
		const playlist = page.locator('[data-testid="playlist-row"]').first();
		await playlist.click({ button: 'right' });
		await expect(page.getByRole('menuitem', { name: 'Rename', exact: true })).toBeVisible();
		await expect(page.getByRole('menuitemcheckbox', { name: 'Forbid duplicates' })).toBeVisible();
		await closeMenus(page);

		const header = page.locator('[data-testid="playlist-folder"]');
		await header.click({ button: 'right' });
		await expect(page.locator(MENU)).toContainText('New playlist');
		// Inventory row 7: no dead Rename/Delete/Duplicate/Reveal on the header.
		for (const name of ['Rename', 'Delete', 'Duplicate', 'Reveal in tree']) {
			await expect(page.getByRole('menuitem', { name, exact: true })).toHaveCount(0);
		}
		await closeMenus(page);
		expect(await browserMenuSuppressed(header)).toBe(true);
		await closeMenus(page);
	});

	test('deck', async ({ page }) => {
		const deck = page.locator('section[data-deck="1"]').first();
		await deck.click({ button: 'right', position: { x: 12, y: 12 } });
		await expect(page.locator(QUICK_DRAW)).toBeVisible();
		await expect(page.locator(`${QUICK_DRAW} .qd-ctx`)).toContainText('Unload CH1');
		await expect(page.locator(`${QUICK_DRAW} .qd-ctx`)).toContainText(/Play CH1|Pause CH1/);
		await closeMenus(page);
		expect(await browserMenuSuppressed(deck)).toBe(true);
		await closeMenus(page);
	});

	test('hot-cue pad', async ({ page }) => {
		const pad = page.locator('[data-hot-cue-slot="A"]').first();
		await expect(pad).toBeVisible();
		await pad.click({ button: 'right' });
		const ctx = page.locator(`${QUICK_DRAW} .qd-ctx`);
		await expect(ctx).toContainText(/(Set|Move) cue A (at|to) playhead/);
		// Inventory row 13: a pad is not a deck.
		await expect(ctx).not.toContainText('Unload CH');
		await closeMenus(page);
		expect(await browserMenuSuppressed(pad)).toBe(true);
		await closeMenus(page);
	});

	test('waveform row', async ({ page }) => {
		const wave = page.locator('[data-wave-surface="row"]').first();
		await expect(wave).toBeVisible();
		await wave.click({ button: 'right' });
		await expect(page.locator(`${QUICK_DRAW} .qd-ctx`)).toContainText(/Unload CH\d/);
		await closeMenus(page);
		expect(await browserMenuSuppressed(wave)).toBe(true);
		await closeMenus(page);
	});

	test('mixer channel strip', async ({ page }) => {
		const strip = page.locator('[data-mixer-channel="1"]').first();
		await expect(strip).toBeVisible();
		await strip.click({ button: 'right', position: { x: 6, y: 6 } });
		const ctx = page.locator(`${QUICK_DRAW} .qd-ctx`);
		await expect(ctx).toContainText('Reset EQ');
		await expect(ctx).toContainText('Crossfader: A');
		// Codex P2 on #5677: choice items carry the engine's checked state, so
		// the three crossfader choices are checkboxes and exactly one is on.
		const fader = ctx.getByRole('menuitemcheckbox', { name: /Crossfader:/ });
		await expect(fader).toHaveCount(3);
		await expect(ctx.locator('[role="menuitemcheckbox"][aria-checked="true"]', { hasText: 'Crossfader:' })).toHaveCount(1);
		// Inventory row 15: no longer an empty menu.
		await expect(ctx).not.toContainText('No target actions');
		await closeMenus(page);
		expect(await browserMenuSuppressed(strip)).toBe(true);
		await closeMenus(page);
	});

	test('library source tabs: no menu, no browser menu', async ({ page }) => {
		const tabs = page.locator('[data-menu-target="library-source"]').first();
		await expect(tabs).toBeVisible();
		await tabs.click({ button: 'right', position: { x: 4, y: 4 } });
		await expect(page.locator(QUICK_DRAW)).toHaveCount(0);
		await expect(page.locator(MENU)).toHaveCount(0);
		expect(await browserMenuSuppressed(tabs)).toBe(true);
	});

	test('read-only tree (autolists): no menu, no browser menu', async ({ page }) => {
		const tree = page.locator(READONLY_TREE).first();
		await expect(tree).toBeVisible();
		await tree.click({ button: 'right', position: { x: 4, y: 4 } });
		await expect(page.locator(QUICK_DRAW)).toHaveCount(0);
		await expect(page.locator(MENU)).toHaveCount(0);
		expect(await browserMenuSuppressed(tree)).toBe(true);
	});

	test('an expanded Recently Deleted child row gets no menu', async ({ page }) => {
		// Codex P2 on #5677: the child rows render as siblings of the folder
		// header, so they must sit inside the read-only marker too.
		await page.getByTestId('playlist-recently-deleted').click();
		const child = page
			.locator('[data-testid="playlist-recently-deleted-empty"], [data-testid="playlist-deleted-row"]')
			.first();
		await expect(child).toBeVisible();
		// Prime Quick Draw so a zero count below is a classification, not a
		// handler that had not arrived yet.
		await page.locator('.perf-root').first().click({ button: 'right', position: { x: 2, y: 2 } });
		await expect(page.locator(QUICK_DRAW)).toBeVisible();
		await closeMenus(page);
		await child.click({ button: 'right', position: { x: 30, y: 5 } });
		await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
		await expect(page.locator(QUICK_DRAW)).toHaveCount(0);
		await expect(page.locator(MENU)).toHaveCount(0);
		expect(await browserMenuSuppressed(child)).toBe(true);
	});

	test('track chip in the Cmd-K bar', async ({ page }) => {
		await page.getByText('All Tracks', { exact: true }).first().click();
		await expect(page.locator('[data-testid="track-row"]').first()).toBeVisible();
		await page.keyboard.press(`${process.platform === 'darwin' ? 'Meta' : 'Control'}+k`);
		const chip = page.getByTestId('command-bar-row').first();
		await expect(chip).toBeVisible();
		await chip.click({ button: 'right' });
		await expect(page.getByTestId('quick-draw-load-ch1')).toBeVisible();
		await page.keyboard.press('Escape');
		expect(await browserMenuSuppressed(chip)).toBe(true);
	});

	test('empty performance space keeps the Quick Draw launcher', async ({ page }) => {
		const topbar = page.locator('.perf-root').first();
		await topbar.click({ button: 'right', position: { x: 2, y: 2 } });
		await expect(page.locator(QUICK_DRAW)).toBeVisible();
		await expect(page.locator(`${QUICK_DRAW} .qd-ctx`)).toContainText('No target actions');
		await closeMenus(page);
	});
});

test.describe('W28 right-click registry smokes (tall viewport)', () => {
	// The smartlist scroller is 4px tall at 1280x720 (see smartlist-delete.spec.ts).
	test.use({ viewport: { width: 1280, height: 1000 } });

	test('smartlist row', async ({ page }) => {
		const name = `W28 smartlist ${Date.now()}`;
		const create = await page.request.post('/api/v1/smartlists', {
			data: { name, rule: { field: 'rating', op: '>=', value: 0 } }
		});
		expect(create.status()).toBe(201);
		const { id } = (await create.json()) as { id: string };
		try {
			await openPerformance(page);
			await page.getByTestId('library-source-autolists').click();
			const row = page.locator('[data-testid="smartlist-row"]').filter({ has: page.getByText(name, { exact: true }) });
			await expect(row).toBeVisible();
			await row.click({ button: 'right' });
			await expect(page.getByRole('menuitem', { name: 'Duplicate', exact: true })).toBeVisible();
			// Inventory row 8: no dead Reveal on a smartlist.
			await expect(page.getByRole('menuitem', { name: 'Reveal in tree', exact: true })).toHaveCount(0);
			await closeMenus(page);
			expect(await browserMenuSuppressed(row)).toBe(true);
		} finally {
			await page.request.delete(`/api/v1/smartlists/${id}`);
		}
	});
});
